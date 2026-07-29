"""
Source ingestion for Custom Script Projects.

One entry point turns supplied source files into a revision that is on its way to a verdict:
it declares the entrypoints the upload implies, stages the tree, and enqueues validation. The
upload path is its first caller and Data Source reconciliation will be its second, which is why
it takes a project that already exists rather than creating one.

Ordering here is load bearing. A revision freezes the project's enabled declarations into its
entrypoint snapshot at staging time, so a declaration created afterwards would not be part of
the revision that gets validated. The declarations are therefore committed first, and only then
is the tree staged.

The content write deliberately sits outside that transaction. Rolling back around stored bytes
would leave content no revision row names, and nothing reclaims content without a row to
record its cleanup, so a failed write is recorded on the revision as STORAGE_FAILED instead,
where it stays inspectable and retryable.
"""

from django.core.exceptions import ValidationError
from django.db import router, transaction
from django.utils.translation import gettext as _

from .choices import ProjectSourceTypeChoices
from .jobs import RevisionValidationJob
from .models import CustomScriptModule
from .storage import config, service, store
from .storage.paths import normalize_source_path

__all__ = (
    'current_source_tree',
    'ingest_upload',
    'uploaded_source_path',
)

# The first release treats every uploaded file as an executable entrypoint, so only Python
# source can be uploaded. Projects that bundle helper modules are managed through Git.
SOURCE_SUFFIX = '.py'


def uploaded_source_path(filename):
    """
    Return the canonical project-relative path one uploaded file is stored at.

    The name is input, so it goes through the same path policy every stored path obeys, and
    anything that is not Python source is refused here rather than becoming a revision nobody
    can execute. Raises ValidationError, since the immediate caller is a form field.

    Django reduces an uploaded file's name to its basename before a form sees it, so an upload
    can only ever name a file at the project root. The traversal and absolute-path rules below
    still matter, because Data Source reconciliation reaches this with real directory paths.
    """
    try:
        path = normalize_source_path(filename)
    except Exception as error:
        raise ValidationError(str(error)) from error
    if not path.endswith(SOURCE_SUFFIX):
        raise ValidationError(
            _('Only Python source files can be uploaded. "{filename}" is not a {suffix} file.').format(
                filename=filename, suffix=SOURCE_SUFFIX
            )
        )
    return path


def current_source_tree(project):
    """
    Return the project's stored source tree as a mapping of path to bytes, empty if it has none.

    A revision is an immutable whole tree, so adding one file means staging everything that was
    already there plus the new one, and the existing content has to be read back to do that.
    Every file is verified against the manifest on the way out, so a damaged tree raises
    RevisionCorruptError here rather than being carried silently into a new revision.
    """
    revision = project.current_revision
    if revision is None:
        return {}
    return store.read_revision_tree(config.get_storage(), project.storage_key, revision.digest, revision.manifest)


def ingest_upload(project, *, filename, content, base_files=None):
    """
    Declare an uploaded file as an entrypoint, stage it, and enqueue its validation.

    The uploaded name becomes the project-relative source path, canonicalized and confirmed to
    be Python source. base_files carries the project's existing tree, so a later upload stages
    a revision holding what was there plus the new file. Returns the StagedRevision.

    Raises ValidationError for a name the path policy or the source rule refuses, and whatever
    staging raises for a storage failure or a project deleted underneath the write.
    """
    if project.source_type != ProjectSourceTypeChoices.UPLOAD:
        raise ValidationError(
            _(
                'Only projects whose source is uploaded accept file uploads. "{project}" is backed by a Data Source.'
            ).format(project=project)
        )
    path = uploaded_source_path(filename)
    files = dict(base_files or {})
    files[path] = bytes(content)

    using = router.db_for_write(type(project), instance=project)
    with transaction.atomic(using=using):
        _declare_entrypoint(project, path, using)

    staged = service.stage_revision(project, files)
    RevisionValidationJob.enqueue_validation(staged.revision)
    return staged


def _declare_entrypoint(project, path, using):
    """
    Make one path an enabled entrypoint of a project, creating its declaration if needed.

    An uploaded file is always an entrypoint, so re-uploading a path that was turned off turns
    it back on. The row is reused rather than replaced, because Custom Script rows and Job
    history reference the declaration.
    """
    module = CustomScriptModule.objects.using(using).filter(project=project, source_path=path).first()
    if module is None:
        module = CustomScriptModule(project=project, source_path=path, enabled=True)
        module.full_clean()
        module.save(using=using)
        return module
    if not module.enabled:
        module.enabled = True
        module.save(using=using, update_fields=('enabled', 'last_updated'))
    return module
