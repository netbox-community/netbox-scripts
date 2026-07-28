"""
Staging and activation of a Custom Script Project's revisions.

This module resolves the configuration for one operation and passes it down, and it owns the
transaction and lock boundaries of the storage layer. Staging turns a mapping of source files
into a revision row and, for valid content, stored content. Activation moves a
project's active pointer under a row lock, so concurrent activations settle on one winner.
"""

from typing import NamedTuple

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.utils import timezone

from .. import branching, constants
from ..choices import RevisionStatusChoices
from ..models import CustomScriptProject, CustomScriptProjectRevision
from . import config, store
from .exceptions import ActivationError, RevisionCorruptError, StorageError
from .manifest import build_manifest, compute_digest, validate_manifest


class StagedRevision(NamedTuple):
    """The result of one staging call: the revision and whether this call created it."""

    revision: CustomScriptProjectRevision
    created: bool


def stage_revision(project, files):
    """
    Materialize a mapping of source path to content bytes as a revision of a project.

    Success means the tree is stored and matches its manifest, which is MATERIALIZED, not
    VALID. Promotion to VALID belongs to project validation, so nothing staged here can be
    activated until imports, entrypoints, and Script discovery have been checked.

    A content problem never raises. A rejected tree is persisted as an invalid revision with
    a null digest, so its errors and the accepted part of its manifest stay inspectable and
    repeated bad uploads stay distinct. Valid content is content-addressed, so re-staging an
    identical tree returns the existing revision after verifying its stored tree. Only a
    revision whose write never completed is re-driven, so a revision that validation rejected
    is never resurrected. A storage failure records STORAGE_FAILED and re-raises, since it
    is an infrastructure fault rather than a property of the content.

    The files mapping is snapshotted and frozen on entry and every step works from that
    snapshot, so the manifest always describes what was written. Keys must be str and values
    must be bytes-like, and a caller that breaks either gets a TypeError rather than an
    invalid revision, because that is a programming fault and not a property of the content.

    Status transitions are conditional on the row still being inside its write window, so a
    slow or failed writer cannot demote a revision that validation or activation advanced
    concurrently. Such a row is returned as that owner left it.

    The storage lifecycle runs on the default database, where deletion cleanup can be
    recorded, so a project loaded from any other connection is refused before any row or
    content is written. Content staged elsewhere could never be reclaimed.

    Refuses with ImproperlyConfigured when NetBox Branching would not keep these models in the
    main schema, before any row is created, because a revision written under branch-local rows
    would name source another schema also claims.
    """
    branching.require_safe_routing()
    storage = config.get_storage()
    limits = config.get_storage_limits()
    using = _require_default_database(project)
    source_files = _frozen_source(files)
    entries, errors = build_manifest(source_files, limits)
    totals = {
        'manifest': entries,
        'file_count': len(entries),
        'total_size': sum(entry['size'] for entry in entries),
    }

    if errors:
        revision = CustomScriptProjectRevision.objects.using(using).create(
            project=project,
            digest=None,
            status=RevisionStatusChoices.INVALID,
            validation_errors=errors,
            **totals,
        )
        return StagedRevision(revision, True)

    # The caller's instance contributes only its primary key, so a stale or mutated
    # storage_key cannot decide where content is written.
    storage_key = CustomScriptProject.objects.using(using).values_list('storage_key', flat=True).get(pk=project.pk)

    # get_or_create wraps its insert in a savepoint and re-runs the get on IntegrityError,
    # which is exactly the race the partial unique on (project, digest) can lose.
    revision, created = CustomScriptProjectRevision.objects.using(using).get_or_create(
        project=project,
        digest=compute_digest(entries),
        defaults={'status': RevisionStatusChoices.STAGING, **totals},
    )
    while True:
        if revision.status in constants.STORED_REVISION_STATUSES:
            store.verify_revision_tree(storage, storage_key, revision.digest, _validated_manifest(revision))
            return StagedRevision(revision, created)
        if revision.status not in constants.RETRYABLE_REVISION_STATUSES:
            # A revision that validation rejected keeps its errors. Re-staging identical content
            # must neither clear them nor promote it, so the row is returned untouched.
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
        revision.refresh_from_db(using=using)
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
    revision.refresh_from_db(using=using)
    return StagedRevision(revision, created)


def activate_revision(revision):
    """
    Make one revision the active revision of its project and return it, refreshed.

    The owning project is read from the database rather than from the supplied instance, so
    the argument contributes only its primary key and a stale or mutated project reference
    cannot point one project at another project's revision. The stored tree is verified
    before promotion, because a status is not evidence that the backend still holds the
    content. Verification reads and hashes every stored file, so it runs before any row is
    locked and the transaction that moves the pointer stays short. Inside it, the project
    row is locked before the revision row, the same order a project delete takes, so
    concurrent activations serialize rather than deadlock, and the locked row must still
    carry the digest, manifest, and an activatable status the verified snapshot had,
    otherwise activation is refused. Activating the revision that is already active is a
    no-op, checked after ownership and status. Raises ActivationError for a revision that
    has not passed project validation or that changed while its content was being verified,
    and RevisionCorruptError when its stored tree no longer matches its manifest.

    Activation runs on the default database like the rest of the storage lifecycle, so a
    revision loaded from any other connection is refused before anything is read or locked,
    and every promoted revision can later record its deletion cleanup.

    Refuses with ImproperlyConfigured when NetBox Branching would not keep these models in the
    main schema, since a pointer moved inside a branch would not be the pointer the main schema
    serves scripts from.
    """
    branching.require_safe_routing()
    storage = config.get_storage()
    revision_pk = revision.pk
    using = _require_default_database(revision)

    snapshot = CustomScriptProjectRevision.objects.using(using).get(pk=revision_pk)
    project_state = (
        CustomScriptProject.objects.using(using).values('storage_key', 'active_revision_id').get(pk=snapshot.project_id)
    )
    already_active = (
        snapshot.status == RevisionStatusChoices.ACTIVE and project_state['active_revision_id'] == snapshot.pk
    )
    _require_activatable(snapshot, already_active)
    # Verification reads and hashes every stored file and may hold a remote conversation for
    # a while, so it runs before any row is locked. The unlocked reads above decide nothing
    # final, the transaction below re-reads the row against this snapshot.
    store.verify_revision_tree(storage, project_state['storage_key'], snapshot.digest, _validated_manifest(snapshot))

    with transaction.atomic(using=using):
        project = CustomScriptProject.objects.using(using).select_for_update().get(pk=snapshot.project_id)
        locked = (
            CustomScriptProjectRevision.objects.using(using)
            .select_for_update()
            .get(pk=revision_pk, project_id=project.pk)
        )
        if locked.digest != snapshot.digest or locked.manifest != snapshot.manifest:
            raise ActivationError(f'Revision {locked.pk} changed while its stored tree was being verified.')
        already_active = locked.status == RevisionStatusChoices.ACTIVE and project.active_revision_id == locked.pk
        _require_activatable(locked, already_active)
        if already_active:
            return locked

        if project.active_revision_id is not None:
            previous = (
                CustomScriptProjectRevision.objects.using(using)
                .select_for_update()
                .get(pk=project.active_revision_id, project_id=project.pk)
            )
            previous.status = RevisionStatusChoices.RETIRED
            previous.save(using=using, update_fields=('status', 'last_updated'))

        locked.status = RevisionStatusChoices.ACTIVE
        locked.activated = timezone.now()
        locked.save(using=using, update_fields=('status', 'activated', 'last_updated'))

        project.active_revision = locked
        project.save(using=using, update_fields=('active_revision', 'last_updated'))

    return locked


def _require_activatable(revision, already_active):
    """Refuse a revision whose status or digest rules activation out."""
    if not already_active and revision.status not in constants.ACTIVATABLE_REVISION_STATUSES:
        raise ActivationError(f'Revision {revision.pk} has status "{revision.status}" and cannot be activated.')
    # The check constraint makes a digest-less activatable revision impossible to create
    # through the ORM. This stays as the backstop for a row written by raw SQL, and for
    # any future status that is allowed to precede its content.
    if not revision.digest:
        raise ActivationError(f'Revision {revision.pk} has no digest and cannot be activated.')


def _require_default_database(instance):
    """
    Resolve and enforce the one database alias the storage lifecycle runs on.

    Deletion cleanup can record its Job only on the default connection, so staging and
    activation refuse every other alias up front. Content created elsewhere could never
    be reclaimed, because its deletion would be refused.
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
