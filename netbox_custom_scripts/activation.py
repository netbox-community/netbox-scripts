"""
Activation of a Custom Script Project's revisions.

Storage promotes a revision. This module is the domain operation around it, turning the
Custom Scripts a revision recorded at its verdict into rows in the same transaction that
moves the project's active pointer, so a reader sees either the old revision with its old
rows or the new revision with its synchronized rows.

Nothing here imports project code. A fresh import at activation could disagree with the
verdict the revision already carries, and the recorded snapshot is what the verdict was
about.
"""

from .models import CustomScript

__all__ = ('synchronize_scripts',)


def synchronize_scripts(*, project, revision, records, using):
    """
    Bring one project's Custom Script rows in line with a validated discovery snapshot.

    Database work only, inside the transaction the caller opens. A row the snapshot no longer
    names is retired rather than deleted, which keeps its primary key and with it the Job
    history the row has accumulated. A row whose recorded fields already match is left
    untouched, because this runs again on every activation and an unconditional save would log
    a change and queue an event per script per activation.

    The project row is already locked by the promotion, so no row needs a lock of its own, and
    enabled is never written, because it belongs to the administrator.
    """
    rows = {(row.module_path, row.class_name): row for row in project.scripts.using(using)}
    for record in records:
        identity = (record['module_path'], record['class_name'])
        # entrypoint_module_id and entrypoint_path stay in the revision's snapshot. Which
        # entrypoint published a class is provenance, not part of what the class is.
        values = {
            'display_name': record['display_name'],
            'description': record['description'],
            'metadata': record['metadata'],
            'is_retired': False,
            'last_seen_revision': revision,
        }
        row = rows.pop(identity, None)
        if row is None:
            CustomScript.objects.using(using).create(
                project=project,
                module_path=identity[0],
                class_name=identity[1],
                **values,
            )
        else:
            _save_changes(row, values, using)
    for row in rows.values():
        # last_seen_revision keeps naming the last revision that did publish this script.
        _save_changes(row, {'is_retired': True}, using)


def _save_changes(row, values, using):
    """Save one row's differing fields, and issue no statement when none differ."""
    changed = [name for name, value in values.items() if _differs(row, name, value)]
    if not changed:
        return
    for name in changed:
        setattr(row, name, values[name])
    row.save(using=using, update_fields=(*changed, 'last_updated'))


def _differs(row, name, value):
    """Compare one field, without fetching a related object to do it."""
    if name == 'last_seen_revision':
        # Only the publishing path passes this key, and it always passes a saved revision.
        return row.last_seen_revision_id != value.pk
    return getattr(row, name) != value
