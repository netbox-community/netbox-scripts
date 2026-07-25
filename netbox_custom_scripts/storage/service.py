"""
Staging and activation of a Custom Script Project's revisions.

This module resolves the configuration for one operation and passes it down, and it owns the
transaction and lock boundaries of the storage layer. Staging turns a mapping of source files
into a revision row and, for valid content, a revision directory. Activation moves a
project's active pointer under a row lock, so concurrent activations settle on one winner.
"""

from typing import NamedTuple

from django.db import router, transaction
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
    is never resurrected. A filesystem failure records STORAGE_FAILED and re-raises, since it
    is an infrastructure fault rather than a property of the content.

    The files mapping is snapshotted and frozen on entry and every step works from that
    snapshot, so the manifest always describes what was written. Keys must be str and values
    must be bytes-like, and a caller that breaks either gets a TypeError rather than an
    invalid revision, because that is a programming fault and not a property of the content.

    One database alias is resolved from the project and used for every read and write, so a
    project loaded from a non-default connection keeps its revision on that same connection
    and the storage key is never read from a replica that has not caught up.

    Refuses with ImproperlyConfigured when NetBox Branching would not keep these models in the
    main schema, before any row is created, because a revision written under branch-local rows
    would name source another schema also claims.
    """
    branching.require_safe_routing()
    project_root = config.get_project_root()
    limits = config.get_storage_limits()
    using = project._state.db or router.db_for_write(CustomScriptProjectRevision, instance=project)
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
    if revision.status in constants.STORED_REVISION_STATUSES:
        store.verify_revision_tree(project_root, storage_key, revision.digest, _validated_manifest(revision))
        return StagedRevision(revision, created)
    if revision.status not in constants.RETRYABLE_REVISION_STATUSES:
        # A revision that validation rejected keeps its errors. Re-staging identical content
        # must neither clear them nor promote it, so the row is returned untouched.
        return StagedRevision(revision, created)

    # STAGING covers the whole write window, including a retry. VALIDATING belongs to project
    # validation, so the storage layer never occupies it.
    if revision.status != RevisionStatusChoices.STAGING:
        revision.status = RevisionStatusChoices.STAGING
        revision.save(using=using)
    try:
        store.write_staged_revision(
            project_root, storage_key, revision.digest, source_files, _validated_manifest(revision)
        )
    except (OSError, StorageError) as error:
        revision.status = RevisionStatusChoices.STORAGE_FAILED
        revision.validation_errors = [
            {'path': None, 'code': 'storage_write_failed', 'message': f'Unable to store the revision: {error}'}
        ]
        revision.save(using=using)
        raise
    revision.status = RevisionStatusChoices.MATERIALIZED
    revision.validation_errors = []
    revision.save(using=using)
    return StagedRevision(revision, created)


def activate_revision(revision):
    """
    Make one revision the active revision of its project and return it, refreshed.

    The owning project is read from the database rather than from the supplied instance, so
    the argument contributes only its primary key and a stale or mutated project reference
    cannot point one project at another project's revision. The project row is locked before
    the revision row, the same order a project delete takes, so concurrent activations
    serialize rather than deadlock. The stored tree is verified before promotion, because a
    status is not evidence that the filesystem still holds the content. Activating the
    revision that is already active is a no-op, checked after ownership and status. Raises
    ActivationError for a revision that has not passed project validation, and
    RevisionCorruptError when its stored tree no longer matches its manifest.

    Every query and the transaction itself run on one alias, taken from the revision's own
    connection where it has one, so the row locks are held inside the transaction that guards
    them even where a plugin routes these models to a connection of its own.

    Refuses with ImproperlyConfigured when NetBox Branching would not keep these models in the
    main schema, since a pointer moved inside a branch would not be the pointer the main schema
    serves scripts from.
    """
    branching.require_safe_routing()
    project_root = config.get_project_root()
    revision_pk = revision.pk
    # The argument's own connection wins, since a revision loaded from one alias must not be
    # activated against another. Only its alias is taken, never its field values.
    using = revision._state.db or router.db_for_write(CustomScriptProjectRevision, instance=revision)

    with transaction.atomic(using=using):
        project_id = (
            CustomScriptProjectRevision.objects.using(using).values_list('project_id', flat=True).get(pk=revision_pk)
        )
        project = CustomScriptProject.objects.using(using).select_for_update().get(pk=project_id)
        locked = (
            CustomScriptProjectRevision.objects.using(using)
            .select_for_update()
            .get(pk=revision_pk, project_id=project.pk)
        )

        already_active = locked.status == RevisionStatusChoices.ACTIVE and project.active_revision_id == locked.pk
        if not already_active and locked.status not in constants.ACTIVATABLE_REVISION_STATUSES:
            raise ActivationError(f'Revision {locked.pk} has status "{locked.status}" and cannot be activated.')
        # The check constraint makes a digest-less activatable revision impossible to create
        # through the ORM. This stays as the backstop for a row written by raw SQL, and for
        # any future status that is allowed to precede its content.
        if not locked.digest:
            raise ActivationError(f'Revision {locked.pk} has no digest and cannot be activated.')

        store.verify_revision_tree(project_root, project.storage_key, locked.digest, _validated_manifest(locked))

        if already_active:
            return locked

        if project.active_revision_id is not None:
            previous = (
                CustomScriptProjectRevision.objects.using(using)
                .select_for_update()
                .get(pk=project.active_revision_id, project_id=project.pk)
            )
            previous.status = RevisionStatusChoices.RETIRED
            previous.save(using=using)

        locked.status = RevisionStatusChoices.ACTIVE
        locked.activated = timezone.now()
        locked.save(using=using)

        project.active_revision = locked
        project.save(using=using)

    return locked


def _frozen_source(files):
    """
    Return an immutable copy of a source mapping, rejecting anything that is not bytes-like.

    The manifest is hashed from this snapshot and the same snapshot is written, so a value
    that can change in between would leave the stored tree disagreeing with the manifest that
    names it, which the store then refuses and withdraws. Copying a mapping is not enough for
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
