"""
Project validation for stored revisions.

Validation is the lifecycle step between MATERIALIZED and a verdict. It imports every
entrypoint the revision's snapshot names, discovers the Scripts they publish, and records
VALID or INVALID. A verdict is a statement about revision content, so environment trouble
(an unreachable backend, a broken cache, a missing external distribution) never produces
one: the revision reverts to MATERIALIZED and the failure propagates for the job layer to
retry.

Ownership works as a lease. A run claims its revision with a compare-and-swap that records
the owning Job and the claim time, and the claim is reclaimable purely by age, because a
killed worker leaves its Job row running forever. Every final transition filters on the
owning job again, so a stale worker resuming after a reclaim commits nothing, neither
revision fields nor Module rows.

Stored records never carry runtime identities. The private namespace, the storage key, the
digest, and cache paths are stripped from everything persisted to validation_errors, and
the job layer routes its log lines through the same sanitizer.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils import timezone

from .branching import require_safe_routing
from .choices import ModuleDiscoveryStatusChoices, RevisionStatusChoices
from .constants import VALIDATION_LEASE_SECONDS
from .models import CustomScriptModule, CustomScriptProjectRevision
from .runtime.cache import local_revision_dir
from .runtime.discovery import discover_scripts
from .runtime.exceptions import DiscoveryError, EntrypointImportError, InvalidModulePathError
from .runtime.loader import import_entrypoint, revision_import_session, unload_revision
from .runtime.naming import PRIVATE_ROOT, project_module_name, revision_module_name
from .storage import config
from .storage.entrypoints import validate_entrypoint_snapshot
from .storage.exceptions import RevisionCorruptError, StorageError
from .storage.manifest import validate_manifest
from .storage.service import require_default_database
from .utils import source_path_to_dotted_name

__all__ = (
    'ValidationStateError',
    'build_error_sanitizer',
    'classify_entrypoint_error',
    'validate_revision',
)


class ValidationStateError(Exception):
    """Raised when a revision cannot be claimed for validation in its current state."""


def validate_revision(revision, *, job, passthrough=()):
    """
    Drive one revision from MATERIALIZED to a verdict, returning the refreshed revision.

    The claim moves MATERIALIZED to VALIDATING recording the owning job and the start
    time, and can take over an expired lease regardless of what the previous owner's Job
    row says. The verdict input is the validated entrypoint snapshot exclusively, never
    live Module rows, and an empty snapshot is vacuously VALID. Content problems across
    all entries collect into one INVALID verdict with sanitized validation_errors, and
    Module rows named by the snapshot receive their discovery outcomes only after the
    verdict commits under the ownership fence. passthrough lists exception types that
    must escape unwrapped, they roll the claim back and re-raise, as does every
    environment failure. Raises ValidationStateError when the revision is not claimable.
    """
    require_safe_routing()
    require_default_database(revision)
    now = timezone.now()
    lease_horizon = now - timedelta(seconds=VALIDATION_LEASE_SECONDS)
    claimed = CustomScriptProjectRevision.objects.filter(
        Q(status=RevisionStatusChoices.MATERIALIZED)
        | Q(status=RevisionStatusChoices.VALIDATING, validation_started__lt=lease_horizon),
        pk=revision.pk,
    ).update(status=RevisionStatusChoices.VALIDATING, validation_job=job, validation_started=now)
    if not claimed:
        raise ValidationStateError(
            'The revision is not claimable for validation. Only a materialized revision or a '
            'validation whose lease has expired can be claimed.'
        )
    revision.refresh_from_db()

    try:
        entries = validate_entrypoint_snapshot(revision.entrypoint_snapshot, revision.entrypoint_digest)
        validate_manifest(revision.manifest, revision.digest)
    except RevisionCorruptError:
        _revert_to_materialized(revision, job)
        raise

    if not entries:
        _finalize(revision, job, RevisionStatusChoices.VALID, [])
        revision.refresh_from_db()
        return revision

    storage_key = str(revision.project.storage_key)
    digest = revision.digest
    revision_prefix = revision_module_name(storage_key, digest)
    revision_modules = _top_level_module_names(revision.manifest)
    sanitize = build_error_sanitizer(storage_key, digest)
    failures = []
    outcomes = {}
    identities = {}
    try:
        storage = config.get_storage()
        with revision_import_session(storage_key, digest):
            try:
                for entry in entries:
                    source_path = entry['source_path']
                    try:
                        module = import_entrypoint(
                            storage_key,
                            digest,
                            source_path,
                            storage=storage,
                            manifest=revision.manifest,
                            passthrough=passthrough,
                        )
                        found = discover_scripts(
                            module, project_key=revision.project.key, revision_prefix=revision_prefix
                        )
                    except (DiscoveryError, InvalidModulePathError) as error:
                        outcomes[source_path] = _content_failure(failures, sanitize, source_path, error)
                        continue
                    except EntrypointImportError as error:
                        classification = classify_entrypoint_error(
                            error, revision_prefix=revision_prefix, revision_modules=revision_modules
                        )
                        if classification != 'content':
                            raise
                        outcomes[source_path] = _content_failure(failures, sanitize, source_path, error)
                        continue
                    outcomes[source_path] = _collect_identities(failures, identities, sanitize, source_path, found)
            finally:
                unload_revision(storage_key, digest)
    except BaseException:
        _revert_to_materialized(revision, job)
        raise

    status = RevisionStatusChoices.INVALID if failures else RevisionStatusChoices.VALID
    if _finalize(revision, job, status, failures):
        _persist_module_results(revision, entries, outcomes, failures)
    revision.refresh_from_db()
    return revision


def classify_entrypoint_error(error, *, revision_prefix, revision_modules=frozenset()):
    """
    Return 'content' or 'environment' for one wrapped entrypoint import failure.

    The loader chains the original exception, so the cause is what gets judged. Content
    means the revision itself can never import: bad syntax, a reference to a revision
    module that does not exist, or project code raising at import time. Environment means
    the process could not give the revision a fair try: a missing external distribution,
    backend or cache trouble, or host I/O failure. A missing cause is the loader's own
    manifest-membership refusal, which is content by construction.

    revision_modules names the top-level modules the revision's own tree ships, which is what
    separates an author's absolute import of their own helper from a distribution this host
    does not have. Without it both arrive as an unprefixed ModuleNotFoundError.
    """
    cause = error.__cause__
    if cause is None:
        return 'content'
    if isinstance(cause, SyntaxError):
        return 'content'
    if isinstance(cause, ImportError):
        # A from-import that names a missing revision member raises with the package
        # itself as the name, so the prefix match covers that shape too.
        name = cause.name or ''
        if name == revision_prefix or name.startswith(f'{revision_prefix}.'):
            return 'content'
        # "import helpers" where the revision ships helpers.py, rather than "from . import".
        return 'content' if name.split('.')[0] in revision_modules else 'environment'
    if isinstance(cause, (StorageError, OSError)):
        return 'environment'
    return 'content'


def build_error_sanitizer(storage_key, digest):
    """
    Return a callable that strips one revision's runtime identities out of text.

    Module references rewrite project-relative, and the namespace, storage key, digest,
    and cache paths reduce to fixed placeholders, so nothing persisted to validation
    errors or a job log identifies storage or runtime internals.
    """
    revision_prefix = revision_module_name(storage_key, digest)
    project_name = project_module_name(storage_key)
    cache_dir = str(local_revision_dir(storage_key, digest))
    replacements = (
        (f'{cache_dir}/', ''),
        (cache_dir, '<revision>'),
        (f'{revision_prefix}.', ''),
        (revision_prefix, '<revision>'),
        (f'{project_name}.', ''),
        (project_name, '<project>'),
        (PRIVATE_ROOT, '<runtime>'),
        (str(storage_key), '<storage>'),
        (uuid.UUID(str(storage_key)).hex, '<storage>'),
        (digest, '<digest>'),
    )

    def sanitize(text):
        if text is None:
            return None
        for private, public in replacements:
            text = text.replace(private, public)
        return text

    return sanitize


def _top_level_module_names(manifest):
    """Return the top-level module names a revision's own tree makes importable."""
    names = set()
    for entry in manifest:
        try:
            dotted = source_path_to_dotted_name(entry['path'])
        except ValidationError:
            continue
        if dotted:
            names.add(dotted.split('.')[0])
    return names


def _content_failure(failures, sanitize, source_path, error):
    """Record one sanitized content failure, returning the failed module outcome."""
    if isinstance(error, EntrypointImportError):
        detail = error.detail
        record = {
            'source_path': source_path,
            'code': detail.get('code'),
            'message': sanitize(detail.get('message')),
            'exception_type': detail.get('exception_type'),
            'traceback': sanitize(detail.get('traceback')),
        }
    else:
        record = {
            'source_path': source_path,
            'code': error.code,
            'message': sanitize(str(error)),
            'exception_type': None,
            'traceback': None,
        }
    failures.append(record)
    return ModuleDiscoveryStatusChoices.FAILED


def _collect_identities(failures, identities, sanitize, source_path, found):
    """
    Fold one entry's discoveries into the revision-wide identity map.

    One class re-exported by several entrypoints is one publication, but two different
    classes sharing one logical identity are a content failure charged to the entry that
    surfaced the collision.
    """
    outcome = ModuleDiscoveryStatusChoices.DISCOVERED
    for item in found:
        identity = (item.logical_module, item.name)
        existing = identities.get(identity)
        if existing is None:
            identities[identity] = item.cls
        elif existing is not item.cls:
            failures.append(
                {
                    'source_path': source_path,
                    'code': 'duplicate_identity',
                    'message': sanitize(f'Two script classes publish as "{item.logical_module}.{item.name}".'),
                    'exception_type': None,
                    'traceback': None,
                }
            )
            outcome = ModuleDiscoveryStatusChoices.FAILED
    return outcome


def _finalize(revision, job, status, validation_errors):
    """Commit one verdict under the ownership fence, reporting whether this run still owned it."""
    updated = CustomScriptProjectRevision.objects.filter(
        pk=revision.pk,
        status=RevisionStatusChoices.VALIDATING,
        validation_job=job,
    ).update(status=status, validation_errors=validation_errors)
    return bool(updated)


def _revert_to_materialized(revision, job):
    """Give the claim back after environment trouble, under the same ownership fence."""
    CustomScriptProjectRevision.objects.filter(
        pk=revision.pk,
        status=RevisionStatusChoices.VALIDATING,
        validation_job=job,
    ).update(status=RevisionStatusChoices.MATERIALIZED, validation_job=None, validation_started=None)


def _persist_module_results(revision, entries, outcomes, failures):
    """
    Record discovery outcomes on the Module rows the snapshot named.

    Matching is by primary key, project, and unchanged source path, so a row deleted,
    moved, or renamed since staging is skipped, the snapshot stays the authoritative
    record of what was validated. A row already carrying a newer run's outcome is left
    alone, while a row this same revision wrote before may refresh, which an
    expired-lease revalidation needs.
    """
    messages = {}
    for record in failures:
        messages.setdefault(record['source_path'], record['message'] or '')
    for entry in entries:
        source_path = entry['source_path']
        outcome = outcomes.get(source_path)
        if outcome is None:
            continue
        failed = outcome == ModuleDiscoveryStatusChoices.FAILED
        CustomScriptModule.objects.filter(
            Q(last_discovered_revision__isnull=True)
            | Q(last_discovered_revision=revision)
            | Q(last_discovered_revision__validation_started__lt=revision.validation_started),
            pk=entry['module'],
            project_id=revision.project_id,
            source_path=source_path,
        ).update(
            discovery_status=outcome,
            discovery_error=messages.get(source_path, '') if failed else '',
            last_discovered_revision=revision,
        )
