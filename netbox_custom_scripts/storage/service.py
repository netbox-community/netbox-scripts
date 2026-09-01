"""
Staging and activation of a Custom Script Project's revisions.

This module resolves the configuration for one operation and passes it down, and it owns the
transaction and lock boundaries of the storage layer. Staging turns a mapping of source files
into a revision row and, for valid content, stored content. Activation moves a
project's active pointer under a row lock, so concurrent activations settle on one winner.

Every operation that touches stored content holds the project lock from locks.py while it does,
which is what stops cleanup, another stager, and activation from interleaving inside a store
the database cannot see. Row locks still guard the pointer move, because they order writes to
the rows themselves.
"""

from typing import NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.utils import timezone

from .. import branching, constants
from ..choices import RevisionStatusChoices
from ..models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from . import config, store
from .entrypoints import build_entrypoint_snapshot, validate_entrypoint_snapshot
from .exceptions import (
    ActivationError,
    ProjectVanishedError,
    RevisionCorruptError,
    RevisionVanishedError,
    StorageError,
)
from .locks import project_lock
from .manifest import build_manifest, compute_digest, validate_manifest

__all__ = (
    'StagedRevision',
    'project_or_vanished',
    'promote_revision',
    'refresh_revision_entrypoints',
    'require_default_database',
    'revision_or_vanished',
    'stage_revision',
)


class StagedRevision(NamedTuple):
    """The result of one staging call: the revision and whether this call created it."""

    revision: CustomScriptProjectRevision
    created: bool


def stage_revision(project, files):
    """
    Materialize a mapping of source path to content bytes as a revision of a project.

    Reaches MATERIALIZED, never VALID: a verdict belongs to project validation. Bad content
    never raises, it lands as an INVALID revision with a null digest, and repeated bad uploads
    stay distinct. Valid content is content-addressed, so an identical tree under an unchanged
    entrypoint configuration returns the existing revision once its stored tree verifies, and a
    changed configuration yields a new identity over the same content. Only a revision whose
    write never completed is re-driven. A row a concurrent owner advanced is returned as that
    owner left it.

    Raises TypeError for a key that is not str or a value that is not bytes-like,
    ProjectVanishedError or RevisionVanishedError for a project deleted underneath the write,
    ImproperlyConfigured for an unsafe database or branching route, and re-raises a storage
    failure after recording STORAGE_FAILED.
    """
    branching.require_safe_routing()
    storage = config.get_storage()
    limits = config.get_storage_limits()
    using = require_default_database(project)
    source_files = _frozen_source(files)
    entries, errors = build_manifest(source_files, limits)
    # The enabled Module declarations are frozen into the revision at staging time, so the
    # verdict validation later reaches keeps meaning when the live declarations change.
    snapshot, entrypoint_digest = build_entrypoint_snapshot(
        CustomScriptModule.objects.using(using).filter(project=project.pk, enabled=True)
    )
    totals = {
        'manifest': entries,
        'file_count': len(entries),
        'total_size': sum(entry['size'] for entry in entries),
        'entrypoint_snapshot': snapshot,
    }

    if errors:
        revision = CustomScriptProjectRevision.objects.using(using).create(
            project=project,
            digest=None,
            status=RevisionStatusChoices.INVALID,
            validation_errors=errors,
            entrypoint_digest=entrypoint_digest,
            **totals,
        )
        return StagedRevision(revision, True)

    # The caller's instance contributes only its primary key, so a stale or mutated
    # storage_key cannot decide where content is written.
    storage_key = project_or_vanished(
        CustomScriptProject.objects.using(using).values_list('storage_key', flat=True), project.pk
    )

    # Held from the identity row through the content write to the status that settles it, so
    # cleanup, another stager, and activation cannot interleave inside a store the database
    # cannot see. The manifest and snapshot built above touch no stored content.
    with project_lock(storage_key, using=using):
        # get_or_create wraps its insert in a savepoint and re-runs the get on IntegrityError,
        # which is exactly the race the partial unique on the identity triple can lose.
        revision, created = CustomScriptProjectRevision.objects.using(using).get_or_create(
            project=project,
            digest=compute_digest(entries),
            entrypoint_digest=entrypoint_digest,
            defaults={'status': RevisionStatusChoices.STAGING, **totals},
        )
        while True:
            if revision.status in constants.STORED_REVISION_STATUSES:
                store.verify_revision_tree(storage, storage_key, revision.digest, _validated_manifest(revision))
                return StagedRevision(revision, created)
            if revision.status not in constants.RETRYABLE_REVISION_STATUSES:
                # A revision that validation rejected keeps its errors. Re-staging identical
                # content must neither clear them nor promote it, so the row is returned
                # untouched.
                return StagedRevision(revision, created)
            # STAGING covers the whole write window, including a retry. VALIDATING belongs to
            # project validation, so the storage layer never occupies it. The transition is
            # conditional, so re-entering the window cannot demote a row another worker advanced
            # between the read above and this write.
            claimed = (
                CustomScriptProjectRevision.objects.using(using)
                .filter(pk=revision.pk, status__in=constants.RETRYABLE_REVISION_STATUSES)
                .update(status=RevisionStatusChoices.STAGING, last_updated=timezone.now())
            )
            if claimed:
                break
            _refresh_surviving(revision, using)
        try:
            store.write_revision(storage, storage_key, revision.digest, source_files, _validated_manifest(revision))
        except (OSError, StorageError) as error:
            # Only a row still inside its write window takes the failure. One a concurrent owner
            # has advanced keeps that owner's word, and the error still propagates either way.
            CustomScriptProjectRevision.objects.using(using).filter(
                pk=revision.pk, status=RevisionStatusChoices.STAGING
            ).update(
                status=RevisionStatusChoices.STORAGE_FAILED,
                validation_errors=[
                    {'path': None, 'code': 'storage_write_failed', 'message': f'Unable to store the revision: {error}'}
                ],
                last_updated=timezone.now(),
            )
            raise
        # The same conditional write settles success, so a slow writer cannot pull a revision
        # back from VALID or ACTIVE. A write that lost its window self-heals on the next call,
        # which re-verifies the stored tree.
        CustomScriptProjectRevision.objects.using(using).filter(
            pk=revision.pk, status=RevisionStatusChoices.STAGING
        ).update(status=RevisionStatusChoices.MATERIALIZED, validation_errors=[], last_updated=timezone.now())
        _refresh_surviving(revision, using)
        return StagedRevision(revision, created)


def refresh_revision_entrypoints(revision):
    """
    Stage a revision's stored content under the project's current entrypoint configuration.

    A verdict binds to the entrypoint snapshot a revision froze at staging time, so fixing a
    Module declaration cannot revalidate an existing row. This creates or returns the row for
    the same stored content under the configuration as it is now, without the content being
    uploaded again. The source revision only needs a digest, so even an INVALID verdict on
    the old configuration stays untouched while its content gets a fresh candidate. Returns
    a StagedRevision whose created flag says whether this configuration was already staged.

    Raises ValueError for a revision with no digest, since a rejected staging attempt stored
    no content to refresh, RevisionCorruptError when the stored tree no longer matches the
    manifest the new row would copy, and ProjectVanishedError or RevisionVanishedError for a
    row deleted underneath the read.

    Guards match the rest of the lifecycle: branching routing and the default database are
    enforced before any row is read or written.
    """
    branching.require_safe_routing()
    storage = config.get_storage()
    using = require_default_database(revision)

    source = revision_or_vanished(CustomScriptProjectRevision.objects.using(using), revision.pk)
    if not source.digest:
        raise ValueError(f'Revision {source.pk} has no digest, so there is no stored content to refresh.')
    manifest = _validated_manifest(source)
    snapshot, entrypoint_digest = build_entrypoint_snapshot(
        CustomScriptModule.objects.using(using).filter(project=source.project_id, enabled=True)
    )
    storage_key = project_or_vanished(
        CustomScriptProject.objects.using(using).values_list('storage_key', flat=True), source.project_id
    )
    # The new row claims content this call has just proven present, so the verification and the
    # row that depends on it happen under one hold. Otherwise cleanup could reclaim the tree in
    # between and leave a MATERIALIZED revision naming nothing.
    with project_lock(storage_key, using=using):
        store.verify_revision_tree(storage, storage_key, source.digest, manifest)

        candidate, created = CustomScriptProjectRevision.objects.using(using).get_or_create(
            project_id=source.project_id,
            digest=source.digest,
            entrypoint_digest=entrypoint_digest,
            defaults={
                'status': RevisionStatusChoices.MATERIALIZED,
                'manifest': manifest,
                'file_count': source.file_count,
                'total_size': source.total_size,
                'entrypoint_snapshot': snapshot,
            },
        )
        return StagedRevision(candidate, created)


def promote_revision(revision, *, on_promote):
    """
    Make one revision the active revision of its project and return it, refreshed.

    on_promote is called as on_promote(project=..., revision=..., using=...) inside the
    transaction that moves the pointer, after both row locks and the identity recheck, and
    before the pointer moves. It may do database work on the alias it is given and nothing
    else, no storage reads, no imports, no network calls. It is required rather than optional,
    so that an active revision and whatever is derived from it always change together. It also
    runs on the already-active path, so re-promoting the current revision repairs whatever a
    caller derives from it while moving nothing.

    The supplied revision contributes only its primary key. The stored tree is verified first,
    because a status is not evidence that the backend still holds the content.

    Raises ActivationError for a revision that has not passed project validation or that
    changed while its content was being verified, RevisionCorruptError when its stored tree no
    longer matches its manifest, ProjectVanishedError or RevisionVanishedError for a row
    deleted underneath the read, and ImproperlyConfigured for an unsafe database or branching
    route.
    """
    branching.require_safe_routing()
    storage = config.get_storage()
    revision_pk = revision.pk
    using = require_default_database(revision)

    snapshot = revision_or_vanished(CustomScriptProjectRevision.objects.using(using), revision_pk)
    project_state = project_or_vanished(
        CustomScriptProject.objects.using(using).values('storage_key', 'active_revision_id'), snapshot.project_id
    )
    already_active = (
        snapshot.status == RevisionStatusChoices.ACTIVE and project_state['active_revision_id'] == snapshot.pk
    )
    _require_activatable(snapshot, already_active)
    # The entrypoint snapshot is persisted input that execution will trust, so it is checked
    # against its own digest here, at the same position the manifest gets its return-trip
    # check, before any row is locked.
    validate_entrypoint_snapshot(snapshot.entrypoint_snapshot, snapshot.entrypoint_digest)
    # The project lock covers the verification and the pointer move together, so nothing
    # reclaims or restages this tree between proving it present and promoting it. It is a
    # session lock rather than a transactional one precisely so the verification below can take
    # as long as the backend needs without holding a transaction open.
    with project_lock(project_state['storage_key'], using=using):
        # Verification reads and hashes every stored file and may hold a remote conversation for
        # a while, so it runs before any row is locked. The unlocked reads above decide nothing
        # final, the transaction below re-reads the row against this snapshot.
        store.verify_revision_tree(
            storage, project_state['storage_key'], snapshot.digest, _validated_manifest(snapshot)
        )
        return _promote(snapshot, revision_pk, using, on_promote)


def _promote(snapshot, revision_pk, using, on_promote):
    """Move a project's active pointer to one verified revision, under the row locks."""
    with transaction.atomic(using=using):
        # Project row before revision row, the same order a project delete takes, so concurrent
        # activations serialize rather than deadlock.
        project = project_or_vanished(CustomScriptProject.objects.using(using).select_for_update(), snapshot.project_id)
        locked = revision_or_vanished(
            CustomScriptProjectRevision.objects.using(using).select_for_update(),
            revision_pk,
            project_id=project.pk,
        )
        # The snapshot and its digest are compared, not just the digest: a swap leaving the digest
        # field untouched would activate content the return-trip check never covered. The recorded
        # scripts are compared for the same reason, the callback is about to derive rows from them.
        if (
            locked.digest != snapshot.digest
            or locked.manifest != snapshot.manifest
            or locked.entrypoint_digest != snapshot.entrypoint_digest
            or locked.entrypoint_snapshot != snapshot.entrypoint_snapshot
            or locked.discovered_scripts != snapshot.discovered_scripts
        ):
            raise ActivationError(f'Revision {locked.pk} changed while its stored tree was being verified.')
        already_active = locked.status == RevisionStatusChoices.ACTIVE and project.active_revision_id == locked.pk
        _require_activatable(locked, already_active)
        # Before the early return, so re-promoting the revision already in force repairs
        # whatever the callback derives from it.
        on_promote(project=project, revision=locked, using=using)
        if already_active:
            return locked

        if project.active_revision_id is not None:
            previous = revision_or_vanished(
                CustomScriptProjectRevision.objects.using(using).select_for_update(),
                project.active_revision_id,
                project_id=project.pk,
            )
            previous.status = RevisionStatusChoices.RETIRED
            previous.save(using=using, update_fields=('status', 'last_updated'))

        locked.status = RevisionStatusChoices.ACTIVE
        locked.activated = timezone.now()
        locked.save(using=using, update_fields=('status', 'activated', 'last_updated'))

        project.active_revision = locked
        project.save(using=using, update_fields=('active_revision', 'last_updated'))

    return locked


def project_or_vanished(query, project_id):
    """Return one shaped project read, reporting a concurrent deletion as such."""
    try:
        return query.get(pk=project_id)
    except CustomScriptProject.DoesNotExist as error:
        raise ProjectVanishedError(f'Project {project_id} was deleted while its content was being changed.') from error


def revision_or_vanished(query, revision_pk, **filters):
    """Return one shaped revision read, reporting a concurrent deletion as such."""
    try:
        return query.get(pk=revision_pk, **filters)
    except CustomScriptProjectRevision.DoesNotExist as error:
        raise RevisionVanishedError(
            f'Revision {revision_pk} was deleted while its content was being changed.'
        ) from error


def _refresh_surviving(revision, using):
    """
    Re-read a revision's row, reporting a concurrent deletion as such.

    Deleting a project cascades its revisions away and takes no project lock, so a staging
    call inside its write window can reach here with no row left to read. Django would raise a
    bare DoesNotExist naming nothing a caller can act on, so it becomes the typed error
    instead.
    """
    try:
        revision.refresh_from_db(using=using)
    except CustomScriptProjectRevision.DoesNotExist as error:
        raise RevisionVanishedError(
            f'Revision {revision.pk} was deleted while its content was being staged.'
        ) from error


def _require_activatable(revision, already_active):
    """Refuse a revision whose status or digest rules activation out."""
    if not already_active and revision.status not in constants.ACTIVATABLE_REVISION_STATUSES:
        raise ActivationError(f'Revision {revision.pk} has status "{revision.status}" and cannot be activated.')
    # The check constraint makes a digest-less activatable revision impossible to create
    # through the ORM. This stays as the backstop for a row written by raw SQL, and for
    # any future status that is allowed to precede its content.
    if not revision.digest:
        raise ActivationError(f'Revision {revision.pk} has no digest and cannot be activated.')


def require_default_database(instance):
    """
    Resolve and enforce the one database alias the storage lifecycle runs on.

    Deletion cleanup can record its Job only on the default connection, so staging,
    refresh, activation, and validation refuse every other alias up front. Content
    created elsewhere could never be reclaimed, because its deletion would be refused.
    """
    using = instance._state.db or router.db_for_write(CustomScriptProjectRevision, instance=instance)
    if using != DEFAULT_DB_ALIAS:
        raise ImproperlyConfigured(
            f'Custom Script Project storage operations run on the "{DEFAULT_DB_ALIAS}" database only. '
            f'This operation arrived on "{using}", where stored content could never be reclaimed, '
            'because deletion cleanup is recorded on the default connection.'
        )
    return using


def _frozen_source(files):
    """
    Return an immutable copy of a source mapping, rejecting anything that is not bytes-like.

    The manifest is hashed from this snapshot and the same snapshot is written, so a value
    that can change in between would leave the stored tree disagreeing with the manifest that
    names it, which the store then refuses. Copying a mapping is not enough for
    that, because a bytearray or memoryview value stays shared, so each value is copied to
    bytes here. A str value is a caller fault rather than bad content, since whatever produced
    it already chose an encoding, so it raises instead of becoming an invalid revision.
    """
    frozen = {}
    for path, content in files.items():
        if not isinstance(path, str):
            raise TypeError(f'Source paths must be strings, got {type(path).__name__}.')
        if not isinstance(content, (bytes, bytearray, memoryview)):
            raise TypeError(f'Source content for "{path}" must be bytes-like, got {type(content).__name__}.')
        frozen[path] = bytes(content)
    return frozen


def _validated_manifest(revision):
    """
    Return a revision's manifest once the row is confirmed to still describe itself.

    A manifest read back from a row is input again, so it is checked against its own digest
    before use, and the row's counters are confirmed to summarize the entries they claim to.
    The counters live on the row rather than in the manifest, which is why this check belongs
    here and not in the store. Raises RevisionCorruptError when any of them disagree.
    """
    validate_manifest(revision.manifest, revision.digest)
    reasons = []
    if revision.file_count != len(revision.manifest):
        reasons.append('file_count_mismatch')
    if revision.total_size != sum(entry['size'] for entry in revision.manifest):
        reasons.append('total_size_mismatch')
    if reasons:
        raise RevisionCorruptError(f'Revision {revision.pk} does not match its own manifest.', reasons)
    return revision.manifest
