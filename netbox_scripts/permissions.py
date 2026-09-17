"""Object-level authorization for parent-driven writes on Script Projects and their files."""

from django.db import DEFAULT_DB_ALIAS
from django.utils.translation import gettext_lazy as _

from utilities.exceptions import PermissionsViolation

from .constants import AUTHORIZED_SOURCE_MOVES, GATED_SOURCE_FIELDS
from .models import ScriptFile, ScriptProject
from .validators import normalize_data_path

ACTIVATE_PERMISSION = 'netbox_scripts.activate_scriptproject'

SOURCE_REFUSAL = _('Changing this field requires the Script Project activate permission.')


def validate_script_file_permissions(user, *, created=(), changed=(), using):
    """Refuse rows outside the user's add or change scope, inside the caller's transaction."""
    if user is None:
        return
    for action, keys in (('add', created), ('change', changed)):
        keys = set(keys)
        if keys and ScriptFile.objects.using(using).restrict(user, action).filter(pk__in=keys).count() != len(keys):
            raise PermissionsViolation()


def moved_source_fields(pk, submitted, *, using=DEFAULT_DB_ALIAS):
    """Return the gated source fields whose submitted value differs from the stored row."""
    if not any(field in submitted for field in GATED_SOURCE_FIELDS):
        return ()
    # The stored row: every caller arrives after the submitted values are already on the instance.
    stored = (
        ScriptProject.objects.using(using)
        .filter(pk=pk)
        .values('activation_policy', 'data_path', 'data_source_id')
        .first()
    )
    if stored is None:
        return ()
    moved = []
    for field in GATED_SOURCE_FIELDS:
        if field not in submitted:
            continue
        value = submitted[field]
        if field == 'data_source':
            # A resolved object or a bare id, whichever the caller holds without a query.
            value = getattr(value, 'pk', value)
            current = stored['data_source_id']
        elif field == 'data_path':
            # Canonicalized on the way in, so a raw comparison reads a trailing separator as a move.
            value = normalize_data_path(value or '')
            current = stored[field]
        else:
            current = stored[field]
        if value != current:
            moved.append(field)
    return tuple(moved)


def unpermitted_source_moves(user, project, submitted):
    """
    Return the gated source fields this caller may not move, recording the ones it may.

    An empty result authorizes the save. The recorded set is what ScriptProject.save() compares
    its own locked read against, so a field that moves after this runs is refused there rather
    than written back from an instance loaded before it moved.
    """
    if user is None:
        return ()
    moved = moved_source_fields(project.pk, submitted)
    if moved and not user.has_perm(ACTIVATE_PERMISSION, obj=project):
        return moved
    setattr(project, AUTHORIZED_SOURCE_MOVES, frozenset(moved))
    return ()


def refuse_unpermitted_source_change(user, project, submitted):
    """Raise PermissionsViolation when a caller who may not activate the Project moves a source field."""
    if refused := unpermitted_source_moves(user, project, submitted):
        error = PermissionsViolation()
        # message is a class attribute, so a constructor argument would not reach a reader of it.
        error.message = _('Changing {fields} requires the Script Project activate permission.').format(
            fields=', '.join(refused)
        )
        raise error
