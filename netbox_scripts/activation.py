"""
Activation of a Script Project's revisions.

Storage promotes a revision. This module is the domain operation around it, turning the
Scripts a revision recorded at its verdict into rows in the same transaction that
moves the project's active pointer, so a reader sees either the old revision with its old
rows or the new revision with its synchronized rows.

Nothing here imports project code. A fresh import at activation could disagree with the
verdict the revision already carries, and the recorded snapshot is what the verdict was
about.
"""

from typing import NamedTuple

from django.db import transaction

from . import branching
from .choices import RevisionStatusChoices
from .models import NetBoxScript, ScriptProject, ScriptProjectRevision
from .runtime.exceptions import ScriptMetadataError
from .runtime.introspection import validate_discovered_scripts
from .storage import service
from .storage.exceptions import ActivationError
from .storage.locks import project_lock
from .storage.service import project_or_vanished, require_default_database, revision_or_vanished

__all__ = (
    'ActivationResult',
    'ScriptSyncResult',
    'activate_revision',
    'deactivate_revision',
    'synchronize_scripts',
)


class ScriptSyncResult(NamedTuple):
    """
    What one synchronization did.

    published is how many Scripts the snapshot names, retired how many rows it stopped
    naming, and written how many rows were actually saved. The three answer different questions
    and only written counts database work, so a caller reporting to an operator has to pick.
    """

    published: int
    retired: int
    written: int


class ActivationResult(NamedTuple):
    """One activation's outcome: the revision now in force, and what its synchronization did."""

    revision: ScriptProjectRevision
    scripts: ScriptSyncResult


def activate_revision(revision, *, automatic=False):
    """
    Make one revision active and publish its Scripts in a single transaction.

    The recorded snapshot is checked twice, deliberately. The check here fails fast, so a
    damaged one never takes the project lock or pays for a full tree verification, and the
    check inside the callback covers the locked row, which is the value rows are actually
    built from. Raises ActivationError, including for a snapshot no build could have produced,
    and RevisionCorruptError when the stored tree no longer matches its manifest.

    Return an ActivationResult, or None when an automatic request is no longer current or permitted.
    """
    _validated_records(revision)
    # The promotion primitive returns the revision and discards what its callback returns, so
    # the count is collected here rather than by widening that contract.
    written = []

    def on_promote(*, project, revision, using):
        written.append(_publish_scripts(project=project, revision=revision, using=using))

    promoted = service.promote_revision(revision, on_promote=on_promote, automatic=automatic)
    if promoted is None:
        return None
    # promote_revision calls the callback exactly once, before its already-active early return.
    (result,) = written
    return ActivationResult(promoted, result)


def deactivate_revision(revision):
    """
    Stand a project down from the revision it is serving, and return that revision, retired.

    The reverse of activation, and the only way to leave a project serving nothing once it has
    served something. Nothing is read from storage and no project code is imported, because a
    project that serves no revision publishes nothing, so there is nothing to describe.

    Its Scripts retire rather than vanish, which is what an empty snapshot means to the
    synchronizer. They keep their primary keys, their Job history, and whatever enabled an
    administrator left them at, so re-activating the revision brings back the same rows.

    Both rows are locked in the order the promotion takes, so a concurrent activation settles
    either side of this rather than interleaving with it. Raises ActivationError when the
    revision is not the one its project is serving.
    """
    branching.require_safe_routing()
    using = require_default_database(revision)
    storage_key = project_or_vanished(
        ScriptProject.objects.using(using).values_list('storage_key', flat=True), revision.project_id
    )
    with project_lock(storage_key, using=using), transaction.atomic(using=using):
        project = project_or_vanished(ScriptProject.objects.using(using).select_for_update(), revision.project_id)
        locked = revision_or_vanished(
            ScriptProjectRevision.objects.using(using).select_for_update(),
            revision.pk,
            project_id=project.pk,
        )
        if project.active_revision_id != locked.pk:
            raise ActivationError(f'Revision {locked.pk} is not the active revision of its project.')

        # No count is threaded out here: deactivation has one outcome, every Script
        # retired, which the confirmation page states before it happens.
        synchronize_scripts(project=project, revision=locked, records=[], using=using)
        locked.status = RevisionStatusChoices.RETIRED
        locked.save(using=using, update_fields=('status', 'last_updated'))
        project.active_revision = None
        project.save(using=using, update_fields=('active_revision', 'last_updated'))

    return locked


def synchronize_scripts(*, project, revision, records, using):
    """
    Bring one project's Script rows in line with a validated discovery snapshot.

    Database work only, inside the transaction the caller opens. A row the snapshot no longer
    names is retired rather than deleted, which keeps its primary key and with it the Job
    history the row has accumulated. A row whose recorded fields already match is left
    untouched, because this runs again on every activation and an unconditional save would log
    a change and queue an event per script per activation.

    The project row is already locked by the promotion, so no row needs a lock of its own, and
    enabled is never written, because it belongs to the administrator.

    Returns a ScriptSyncResult. Each row it writes is a real row change, so inside a request
    it is change logged and queues an OBJECT_UPDATED event attributed to the user who asked for
    the activation. A job applies no request processor, so the same write records neither.
    """
    written = 0
    retired = 0
    rows = {(row.module_path, row.class_name): row for row in project.scripts.using(using)}
    for record in records:
        identity = (record['module_path'], record['class_name'])
        # script_file_id and script_file_path stay in the revision's snapshot. Which
        # script file published a class is provenance, not part of what the class is.
        values = {
            'display_name': record['display_name'],
            'description': record['description'],
            'metadata': record['metadata'],
            'is_retired': False,
            'last_seen_revision': revision,
        }
        row = rows.pop(identity, None)
        if row is None:
            NetBoxScript.objects.using(using).create(
                project=project,
                module_path=identity[0],
                class_name=identity[1],
                **values,
            )
            written += 1
        else:
            written += _save_changes(row, values, using)
    for row in rows.values():
        # last_seen_revision keeps naming the last revision that did publish this script.
        if _save_changes(row, {'is_retired': True}, using):
            written += 1
            retired += 1
    return ScriptSyncResult(published=len(records), retired=retired, written=written)


def _publish_scripts(*, project, revision, using):
    """Bring a project's Script rows in line with the revision being promoted, counting writes."""
    return synchronize_scripts(
        project=project,
        revision=revision,
        records=_validated_records(revision),
        using=using,
    )


def _validated_records(revision):
    """Return a revision's recorded Scripts, reporting a damaged snapshot as a refusal."""
    # Callers already handle one activation failure type, and ScriptMetadataError is not one of
    # them, so an untranslated one would escape as an unhandled error.
    try:
        return validate_discovered_scripts(revision.discovered_scripts)
    except ScriptMetadataError as error:
        raise ActivationError(
            f'Revision {revision.pk} cannot be activated, its recorded Scripts are unusable: {error}'
        ) from error


def _save_changes(row, values, using):
    """Save one row's differing fields, returning whether it wrote and issuing nothing when none differ."""
    changed = [name for name, value in values.items() if _differs(row, name, value)]
    if not changed:
        return False
    for name in changed:
        setattr(row, name, values[name])
    row.save(using=using, update_fields=(*changed, 'last_updated'))
    return True


def _differs(row, name, value):
    """Compare one field, without fetching a related object to do it."""
    if name == 'last_seen_revision':
        # Only the publishing path passes this key, and it always passes a saved revision.
        return row.last_seen_revision_id != value.pk
    return getattr(row, name) != value
