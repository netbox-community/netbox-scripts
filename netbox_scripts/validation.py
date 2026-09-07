"""
Project validation for stored revisions.

Validation is the lifecycle step between MATERIALIZED and a verdict. It imports every
script file the revision's snapshot names, discovers the Scripts they publish, builds each one's
run form to prove it usable, and records VALID or INVALID along with the published set a valid
revision offers. A verdict is a statement about revision content, so environment trouble
(an unreachable backend, a broken cache, a missing external distribution) never produces
one: the revision reverts to MATERIALIZED and the failure propagates for the job layer to
retry.

Ownership works as a lease. A run claims its revision with a compare-and-swap that records
the owning Job and the claim time, and the claim is reclaimable purely by age, because a
killed worker leaves its Job row running forever. Every final transition filters on the
owning job again, so a stale worker resuming after a reclaim commits nothing, neither
revision fields nor Script File rows.

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
from .choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from .constants import MAX_VALIDATION_ERROR_LENGTH, VALIDATION_LEASE_SECONDS
from .models import ScriptFile, ScriptProjectRevision
from .runtime.cache import local_revision_dir
from .runtime.discovery import discover_scripts, zero_publication_reason
from .runtime.exceptions import DiscoveryError, InvalidModulePathError, ScriptFileImportError, ScriptMetadataError
from .runtime.introspection import describe_script
from .runtime.loader import import_script_file, revision_import_session, unload_revision
from .runtime.naming import PRIVATE_ROOT, project_module_name, revision_module_name
from .storage import config
from .storage.exceptions import RevisionCorruptError, StorageError
from .storage.manifest import validate_manifest
from .storage.script_files import validate_script_file_snapshot
from .storage.service import require_default_database
from .utils import source_path_to_dotted_name

__all__ = (
    'ValidationStateError',
    'build_error_sanitizer',
    'classify_script_file_error',
    'validate_revision',
)


class ValidationStateError(Exception):
    """Raised when a revision cannot be claimed for validation in its current state."""


def validate_revision(revision, *, job, passthrough=()):
    """
    Drive one revision from MATERIALIZED to a verdict, returning the refreshed revision.

    The claim moves MATERIALIZED to VALIDATING recording the owning job and the start
    time, and can take over an expired lease regardless of what the previous owner's Job
    row says. The verdict input is the validated script file snapshot exclusively, never
    live Script File rows, and an empty snapshot is vacuously VALID. Content problems across
    all entries collect into one INVALID verdict with sanitized validation_errors, as does
    a snapshot whose script files all import cleanly and publish nothing between them, under
    the code no_scripts_published. Script File rows named by the snapshot receive their discovery
    outcomes only after the verdict commits under the ownership fence. A VALID verdict carries the described
    publication set, an INVALID one carries an empty set. passthrough lists exception types that
    must escape unwrapped, they roll the claim back and re-raise, as does every
    environment failure. Raises ValidationStateError when the revision is not claimable.
    """
    require_safe_routing()
    require_default_database(revision)
    now = timezone.now()
    lease_horizon = now - timedelta(seconds=VALIDATION_LEASE_SECONDS)
    claimed = ScriptProjectRevision.objects.filter(
        Q(status=RevisionStatusChoices.MATERIALIZED)
        | Q(status=RevisionStatusChoices.VALIDATING, validation_started__lt=lease_horizon),
        pk=revision.pk,
    ).update(
        status=RevisionStatusChoices.VALIDATING,
        validation_job=job,
        validation_started=now,
        validation_error='',
    )
    if not claimed:
        raise ValidationStateError(
            'The revision is not claimable for validation. Only a materialized revision or a '
            'validation whose lease has expired can be claimed.'
        )
    revision.refresh_from_db()

    try:
        entries = validate_script_file_snapshot(revision.script_file_snapshot, revision.script_file_digest)
        validate_manifest(revision.manifest, revision.digest)
    except RevisionCorruptError:
        _revert_to_materialized(revision, job)
        raise

    if not entries:
        _finalize(revision, job, RevisionStatusChoices.VALID, [], [])
        revision.refresh_from_db()
        return revision

    storage_key = str(revision.project.storage_key)
    digest = revision.digest
    revision_prefix = revision_module_name(storage_key, digest)
    revision_modules = _top_level_module_names(revision.manifest)
    sanitize = build_error_sanitizer(storage_key, digest)
    failures = []
    outcomes = {}
    notes = {}
    identities = {}
    records = []
    try:
        storage = config.get_storage()
        with revision_import_session(storage_key, digest):
            try:
                for entry in entries:
                    source_path = entry['source_path']
                    try:
                        module = import_script_file(
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
                    except ScriptFileImportError as error:
                        classification = classify_script_file_error(
                            error, revision_prefix=revision_prefix, revision_modules=revision_modules
                        )
                        if classification != 'content':
                            raise
                        outcomes[source_path] = _content_failure(failures, sanitize, source_path, error)
                        continue
                    if not found:
                        notes[source_path] = sanitize(zero_publication_reason(module))
                        outcomes[source_path] = FileDiscoveryStatusChoices.NO_SCRIPTS
                        continue
                    outcomes[source_path] = _collect_publications(failures, identities, records, sanitize, entry, found)
            finally:
                unload_revision(storage_key, digest)
    except BaseException as error:
        _revert_to_materialized(revision, job, sanitize(_failure_reason(error)))
        raise

    # A revision serving enabled script files and publishing nothing at all cannot run, so it
    # is refused rather than activated. One script file publishing nothing beside a working one
    # is reported on its own row and leaves the verdict alone.
    if not failures and not records:
        failures.extend(
            {
                'source_path': entry['source_path'],
                'code': 'no_scripts_published',
                'message': notes.get(entry['source_path'], ''),
                'exception_type': None,
                'traceback': None,
            }
            for entry in entries
        )
    status = RevisionStatusChoices.INVALID if failures else RevisionStatusChoices.VALID
    published = [] if failures else records
    if _finalize(revision, job, status, failures, published):
        _persist_script_file_results(revision, entries, outcomes, failures, notes)
    revision.refresh_from_db()
    return revision


def classify_script_file_error(error, *, revision_prefix, revision_modules=frozenset()):
    """
    Return 'content' or 'environment' for one wrapped script file import failure.

    The loader chains the original exception, so the cause is what gets judged. Content
    means the revision itself can never import: bad syntax, a reference to a revision
    module that does not exist, or project code raising at import time. Environment means
    the process could not give the revision a fair try: an absent external distribution,
    backend or cache trouble, or host I/O failure. Only an absent module can be that, a name
    missing from a module that did import never is. A missing cause is the loader's own
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
        # Only an absent module can be this host's, so a name missing from a module that did
        # import is content: an author's typo, or the compat tier refusing a legacy import.
        if not isinstance(cause, ModuleNotFoundError):
            return 'content'
        # A relative import of a module the revision does not ship resolves to a private name.
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
    if isinstance(error, ScriptFileImportError):
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
    return FileDiscoveryStatusChoices.FAILED


def _collect_publications(failures, identities, records, sanitize, entry, found):
    """
    Fold one entry's discoveries into the revision-wide identity map and snapshot.

    One class re-exported by several script files is one publication, so it is described once
    and keeps the position of the entry that surfaced it first. Two different classes sharing
    one logical identity are a content failure charged to the entry that surfaced the
    collision, as is a class whose run form cannot be built.
    """
    source_path = entry['source_path']
    outcome = FileDiscoveryStatusChoices.DISCOVERED
    for item in found:
        identity = (item.logical_module, item.name)
        existing = identities.get(identity)
        if existing is not None:
            if existing is not item.cls:
                failures.append(
                    {
                        'source_path': source_path,
                        'code': 'duplicate_identity',
                        'message': sanitize(f'Two script classes publish as "{item.logical_module}.{item.name}".'),
                        'exception_type': None,
                        'traceback': None,
                    }
                )
                outcome = FileDiscoveryStatusChoices.FAILED
            continue
        try:
            record = describe_script(
                item,
                script_file_id=entry['script_file'],
                script_file_path=source_path,
                position=len(records),
            )
        except ScriptMetadataError as error:
            outcome = _content_failure(failures, sanitize, source_path, error)
            continue
        identities[identity] = item.cls
        records.append(record)
    return outcome


def _finalize(revision, job, status, validation_errors, discovered_scripts):
    """Commit one verdict under the ownership fence, reporting whether this run still owned it."""
    # The published set lands in the same statement as the verdict, so a run that lost its
    # lease publishes nothing, and no reader ever sees a valid revision without its scripts.
    updated = ScriptProjectRevision.objects.filter(
        pk=revision.pk,
        status=RevisionStatusChoices.VALIDATING,
        validation_job=job,
    ).update(status=status, validation_errors=validation_errors, discovered_scripts=discovered_scripts)
    return bool(updated)


def _failure_reason(error):
    """Return one failure as the text an operator can act on."""
    # ScriptFileImportError's own message names the script file, and its structured record carries
    # the underlying failure, which is the part naming what is actually missing.
    detail = getattr(error, 'detail', None)
    inner = detail.get('message') if isinstance(detail, dict) else None
    return f'{type(error).__name__}: {error} {inner}'.strip() if inner else f'{type(error).__name__}: {error}'


def _revert_to_materialized(revision, job, reason=''):
    """Give the claim back after environment trouble, recording why, under the ownership fence."""
    ScriptProjectRevision.objects.filter(
        pk=revision.pk,
        status=RevisionStatusChoices.VALIDATING,
        validation_job=job,
    ).update(
        status=RevisionStatusChoices.MATERIALIZED,
        validation_job=None,
        validation_started=None,
        validation_error=reason[:MAX_VALIDATION_ERROR_LENGTH],
    )


def _persist_script_file_results(revision, entries, outcomes, failures, notes):
    """
    Record discovery outcomes on the Script File rows the snapshot named.

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
        if outcome == FileDiscoveryStatusChoices.FAILED:
            message = messages.get(source_path, '')
        elif outcome == FileDiscoveryStatusChoices.NO_SCRIPTS:
            message = notes.get(source_path, '')
        else:
            message = ''
        ScriptFile.objects.filter(
            Q(last_discovered_revision__isnull=True)
            | Q(last_discovered_revision=revision)
            | Q(last_discovered_revision__validation_started__lt=revision.validation_started),
            pk=entry['script_file'],
            project_id=revision.project_id,
            source_path=source_path,
        ).update(
            discovery_status=outcome,
            discovery_error=message,
            last_discovered_revision=revision,
        )
