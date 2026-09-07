"""
Source ingestion for Script Projects.

This module turns supplied source files into a revision that is on its way to a verdict: it
declares the script files the source implies, stages the tree, and enqueues validation. Upload and
Data Source reconciliation are its two callers, which is why an entry point here takes a project
that already exists rather than creating one.

The two differ in what the source implies. An uploaded file is a script file unless its caller
says otherwise, which only migration does, for a built-in module that published no Script. A
synchronized directory declares nothing, because a Python file that appears in a repository is a
candidate somebody selects rather than something to publish on arrival.

Ordering here is load bearing. A revision freezes the project's enabled declarations into its
script file snapshot at staging time, so a declaration created afterwards would not be part of
the revision that gets validated. The declarations are therefore committed first, and only then
is the tree staged.

The content write deliberately sits outside that transaction. Rolling back around stored bytes
would leave content no revision row names, and nothing reclaims content without a row to
record its cleanup, so a failed write is recorded on the revision as STORAGE_FAILED instead,
where it stays inspectable and retryable.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext as _

from . import branching
from .choices import ProjectSourceTypeChoices, RevisionStatusChoices
from .jobs import RevisionValidationJob
from .models import ScriptFile
from .storage import config, service, store
from .storage.exceptions import UnsafePathError
from .storage.paths import normalize_source_path
from .utils import data_source_relative_path

__all__ = (
    'check_upload_conflicts',
    'current_source_tree',
    'declare_script_file',
    'ingest_data_source',
    'ingest_upload',
    'uploaded_source_path',
)

# The first release treats every uploaded file as an executable script file, so only Python
# source can be uploaded. Projects that bundle helper modules are managed through Git.
SOURCE_SUFFIX = '.py'


def uploaded_source_path(filename):
    """
    Return the canonical project-relative path one uploaded file is stored at.

    The name is input, so it goes through the same path policy every stored path obeys, and
    anything that is not Python source is refused here rather than becoming a revision nobody
    can execute. Raises ValidationError, since the immediate caller is a form field.

    Django reduces an uploaded file's name to its basename before a form sees it, so an upload
    can only ever name a file at the project root.
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


def check_upload_conflicts(project, path, *, confirm_replace):
    """
    Refuse an upload that would replace content unasked or collide with a sibling declaration.

    Raises ValidationError when the project already holds the canonical path and confirmation
    was not given, and when the path collides with an existing declaration by letter case or by
    module name. Only the manifest is read, so this costs no content reads.
    """
    # Compared against the canonical path, never the name the client sent, so two files a caller
    # thinks of as different cannot silently replace one another.
    revision = project.latest_stored_revision()
    existing = {entry['path'] for entry in revision.manifest} if revision else set()
    if path in existing and not confirm_replace:
        raise ValidationError(
            _('This Project already holds "{path}". Confirm replacement to overwrite its content.').format(path=path)
        )
    if not ScriptFile.objects.filter(project=project, source_path=path).exists():
        candidate = ScriptFile(project=project, source_path=path, enabled=True)
        # A case variant or a name colliding with a sibling module, for example "Deploy.py"
        # against an existing "deploy.py".
        candidate.full_clean()


def current_source_tree(project):
    """
    Return the project's stored source tree as a mapping of path to bytes, empty if it has none.

    A revision is an immutable whole tree, so adding one file means staging everything that was
    already there plus the new one, and the existing content has to be read back to do that.
    The newest stored revision is the tree, whether or not the project serves it, so a file
    added while an earlier revision is still active is carried forward rather than dropped.
    Every file is verified against the manifest on the way out, so a damaged tree raises
    RevisionCorruptError here rather than being carried silently into a new revision.
    """
    revision = project.latest_stored_revision()
    if revision is None:
        return {}
    return store.read_revision_tree(config.get_storage(), project.storage_key, revision.digest, revision.manifest)


def ingest_upload(project, *, filename, content, base_files=None, declare=True, activate_once=False):
    """
    Stage an uploaded file as a revision, declare it unless told not to, and enqueue validation.

    The uploaded name becomes the project-relative source path, canonicalized and confirmed to
    be Python source. base_files carries the project's existing tree, so a later upload stages
    a revision holding what was there plus the new file. Returns the StagedRevision.

    Validation is enqueued only for a revision that is still claimable, so re-uploading content
    that already reached a verdict resolves to that revision and leaves it alone.

    Pass declare=False to stage the file without declaring it, so the project gains the content
    and no script file. Pass activate_once=True to activate this one revision on a valid verdict
    whatever the project's standing policy says.

    Raises ValidationError for a name the path policy or the source rule refuses,
    ImproperlyConfigured for an unsafe routing or a non-default alias, and whatever staging
    raises for a storage failure or a project deleted underneath the write.
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

    # The same pair stage_revision opens with, taken here because the declaration commits first
    # and a refusal after it would leave a stray script file behind.
    branching.require_safe_routing()
    using = service.require_default_database(project)
    if declare:
        with transaction.atomic(using=using):
            declare_script_file(project, path, using)

    staged = service.stage_revision(project, files)
    # Content addressing means identical bytes resolve to the existing revision, carrying whatever
    # verdict it already holds. Only MATERIALIZED is claimable, so enqueueing any other status
    # would fail a job over an upload that changed nothing.
    if staged.revision.status == RevisionStatusChoices.MATERIALIZED:
        RevisionValidationJob.enqueue_validation(staged.revision, activate_once=activate_once)
    return staged


def ingest_data_source(project):
    """
    Stage the project's Data Source directory as a revision and enqueue its validation.

    The complete current directory is staged every time, so a file deleted from the source is
    simply absent from the new revision. Nothing is declared, and staging freezes the
    declarations that are already enabled, which is what carries a script file selection across a
    synchronization. Returns the StagedRevision.

    Compiled artifacts are skipped. Every other path the policy refuses is left to staging, which
    records it as an invalid revision naming the path.

    Validation is enqueued only for a revision that is still claimable, so a synchronization that
    changed nothing enqueues nothing. Raises ValidationError for a project whose source is
    uploaded.
    """
    if project.source_type != ProjectSourceTypeChoices.DATA_SOURCE:
        raise ValidationError(
            _('Only projects backed by a Data Source can be reconciled. "{project}" holds uploaded files.').format(
                project=project
            )
        )
    staged = service.stage_revision(project, _data_source_tree(project))
    # An unchanged directory resolves by content addressing to the revision that already holds a
    # verdict, and only a materialized revision is claimable.
    if staged.revision.status == RevisionStatusChoices.MATERIALIZED:
        RevisionValidationJob.enqueue_validation(staged.revision)
    return staged


def _data_source_tree(project):
    """
    Return the project's Data Source directory as a mapping of project-relative path to bytes.

    Compiled artifacts are dropped, and every other unsafe path is kept so that staging records
    it rather than this function hiding it. Keys need no canonicalization of their own, because
    the store canonicalizes on the way in.
    """
    # Two queries on purpose. The scope is decided from paths alone, so the bytes fetched are
    # the project's directory rather than every file on the source, which for a repository
    # holding more than this one project is the difference that matters.
    wanted = {}
    for pk, path in project.data_source.datafiles.values_list('pk', 'path'):
        relative = data_source_relative_path(path, project.data_path)
        if relative is None:
            continue
        # Called for its refusal rather than its result. Bytecode is not source, and a
        # __pycache__ directory can sit at any depth below the configured one, so the check runs
        # per path rather than against the prefix.
        try:
            normalize_source_path(relative)
        except UnsafePathError as error:
            if error.code == 'compiled_artifact':
                continue
        wanted[pk] = relative
    if not wanted:
        return {}
    rows = project.data_source.datafiles.filter(pk__in=wanted).values_list('pk', 'data')
    return {wanted[pk]: content for pk, content in rows}


def declare_script_file(project, path, using):
    """
    Make one path an enabled script file of a project, creating its declaration if needed.

    A path that was turned off is turned back on. The row is reused rather than replaced,
    because Custom Script rows and Job history reference the declaration.
    """
    script_file = ScriptFile.objects.using(using).filter(project=project, source_path=path).first()
    if script_file is None:
        script_file = ScriptFile(project=project, source_path=path, enabled=True)
        script_file.full_clean()
        script_file.save(using=using)
        return script_file
    if not script_file.enabled:
        script_file.enabled = True
        script_file.save(using=using, update_fields=('enabled', 'last_updated'))
    return script_file
