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
from django.utils.translation import gettext as _

from . import branching
from .choices import ProjectSourceTypeChoices, RevisionStatusChoices
from .jobs import RevisionValidationJob
from .models import ScriptFile
from .permissions import validate_script_file_permissions
from .storage import config, service, store
from .storage.exceptions import UnsafePathError
from .storage.locks import project_lock, project_write_lock
from .storage.paths import normalize_source_path
from .utils import data_source_relative_path, source_path_to_dotted_name

__all__ = (
    'check_upload_conflicts',
    'check_upload_preconditions',
    'current_source_tree',
    'declare_script_file',
    'ingest_data_source',
    'ingest_upload',
    'prepare_upload',
    'queue_revision_processing',
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
    source_path_to_dotted_name(path)
    return path


def check_upload_conflicts(project, path, *, confirm_replace):
    """
    Refuse an upload that would replace content unasked or collide with a sibling declaration.

    Raises ValidationError when the project already holds the canonical path or has one staging
    for it and confirmation was not given, and when the path collides with an existing
    declaration by letter case or by module name. No stored content is read.
    """
    # Compared against the canonical path, never the name the client sent, so two files a caller
    # thinks of as different cannot silently replace one another.
    revision = project.latest_stored_revision()
    existing = {entry['path'] for entry in revision.manifest} if revision else set()
    declared = ScriptFile.objects.filter(project=project, source_path=path).exists()
    if not confirm_replace:
        if path in existing:
            raise ValidationError(
                _('This Project already holds "{path}". Confirm replacement to overwrite its content.').format(
                    path=path
                )
            )
        # An uploaded project gains a file only through ingestion, which commits the declaration
        # before it writes any content. The browser path writes that content after the request
        # commits, so a declaration the newest stored manifest does not name is an upload still in
        # flight, or one whose storage write failed. A Data Source project declares paths its own
        # directory supplies, where the same absence means nothing of the kind.
        if declared and project.source_type == ProjectSourceTypeChoices.UPLOAD:
            raise ValidationError(
                _('An upload of "{path}" has not finished storing. Confirm replacement to overwrite it.').format(
                    path=path
                )
            )
    if not declared:
        candidate = ScriptFile(project=project, source_path=path, enabled=True)
        # A case variant or a name colliding with a sibling module, for example "Deploy.py"
        # against an existing "deploy.py".
        candidate.full_clean()


def current_source_tree(project):
    """
    Return the project's stored source tree as a mapping of path to bytes, empty if it has none.

    A revision is an immutable whole tree, so adding one file means staging everything that was
    already there plus the new one, and the existing content has to be read back to do that.
    The most recently accepted source is the tree, even when it reuses an older revision or is
    not active. A file added while an earlier revision is active is therefore carried forward.
    Every file is verified against the manifest on the way out, so a damaged tree raises
    RevisionCorruptError here rather than being carried silently into a new revision.
    """
    revision = project.latest_stored_revision()
    if revision is None:
        return {}
    return store.read_revision_tree(config.get_storage(), project.storage_key, revision.digest, revision.manifest)


def check_upload_preconditions(project):
    """Return the safe write alias, refusing non-upload projects before any declaration write."""
    branching.require_safe_routing()
    using = service.require_default_database(project)
    if project.source_type != ProjectSourceTypeChoices.UPLOAD:
        raise ValidationError(
            _(
                'Only projects whose source is uploaded accept file uploads. "{project}" is backed by a Data Source.'
            ).format(project=project)
        )
    return using


def prepare_upload(project, *, filename, confirm_replace, user=None):
    """Validate an upload and authorize its real declaration inside the caller's transaction."""
    using = check_upload_preconditions(project)
    path = uploaded_source_path(filename)
    with project_write_lock(project.storage_key, using=using):
        check_upload_conflicts(project, path, confirm_replace=confirm_replace)
        declare_script_file(project, path, using, user=user)
    return path


def ingest_upload(project, *, filename, content, declare=True, activate_once=False, user=None):
    """Merge an upload into the accepted source under one lock and enqueue its processing."""
    using = check_upload_preconditions(project)
    path = uploaded_source_path(filename)
    with project_lock(project.storage_key, using=using):
        if declare:
            # Request callers authorize their declaration before committing and pass declare=False.
            # declare=True with no user declares unchecked, which only a trusted caller may do.
            prepare_upload(project, filename=path, confirm_replace=True, user=user)
        files = current_source_tree(project)
        files[path] = bytes(content)
        staged = service.stage_revision(project, files, activate_once=activate_once)
        queue_revision_processing(staged.revision)
        return staged


def ingest_data_source(project):
    """Accept the current Data Source directory under the project lock and enqueue processing."""
    branching.require_safe_routing()
    using = service.require_default_database(project)
    with project_lock(project.storage_key, using=using):
        project.refresh_from_db(using=using)
        if project.source_type != ProjectSourceTypeChoices.DATA_SOURCE:
            raise ValidationError(
                _('Only projects backed by a Data Source can be reconciled. "{project}" holds uploaded files.').format(
                    project=project
                )
            )
        staged = service.stage_revision(project, _data_source_tree(project))
        queue_revision_processing(staged.revision)
        return staged


def queue_revision_processing(revision):
    """Queue validation or activation of a reused verdict, without duplicating an in-flight validation."""
    from .choices import ActivationPolicyChoices
    from .constants import ACTIVATABLE_REVISION_STATUSES
    from .models import ScriptProject

    if revision.status == RevisionStatusChoices.MATERIALIZED:
        return RevisionValidationJob.enqueue_validation(revision)
    if revision.status in ACTIVATABLE_REVISION_STATUSES:
        project = ScriptProject.objects.get(pk=revision.project_id)
        if project.source_activation_pending or project.activation_policy == ActivationPolicyChoices.AUTOMATIC_IF_VALID:
            return RevisionValidationJob.enqueue_validation(revision)
    return None


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


def declare_script_file(project, path, using, *, user=None):
    """Create or enable a declaration, checking both its original and resulting permission scope."""
    with project_write_lock(project.storage_key, using=using):
        script_file = ScriptFile.objects.using(using).filter(project=project, source_path=path).first()
        if script_file is None:
            script_file = ScriptFile(project=project, source_path=path, enabled=True)
            script_file.full_clean()
            script_file.save(using=using)
            validate_script_file_permissions(user, created=(script_file.pk,), using=using)
        elif not script_file.enabled:
            validate_script_file_permissions(user, changed=(script_file.pk,), using=using)
            script_file.enabled = True
            script_file.save(using=using, update_fields=('enabled', 'last_updated'))
            validate_script_file_permissions(user, changed=(script_file.pk,), using=using)
        return script_file
