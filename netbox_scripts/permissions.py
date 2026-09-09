"""Object-level authorization for parent-driven writes on Script Projects and their files."""

from django.db import DEFAULT_DB_ALIAS
from django.utils.translation import gettext_lazy as _

from utilities.exceptions import PermissionsViolation

# Moving any of these decides what a Project serves next, so each costs activate, not change.
GATED_SOURCE_FIELDS = ('activation_policy', 'data_source', 'data_path')

ACTIVATE_PERMISSION = 'netbox_scripts.activate_scriptproject'

SOURCE_REFUSAL = _('Changing this field requires the Script Project activate permission.')


def validate_script_file_permissions(user, *, created=(), changed=(), using):
    """Refuse rows outside the user's add or change scope, inside the caller's transaction."""
    from .models import ScriptFile

    if user is None:
        return
    for action, keys in (('add', created), ('change', changed)):
        keys = set(keys)
        if keys and ScriptFile.objects.using(using).restrict(user, action).filter(pk__in=keys).count() != len(keys):
            raise PermissionsViolation()


def moved_source_fields(pk, submitted, *, using=DEFAULT_DB_ALIAS):
    """Return the gated source fields whose submitted value differs from the stored row."""
    from .models import ScriptProject
    from .validators import normalize_data_path

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
            value = value.pk if value is not None else None
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


def refuse_unpermitted_source_change(user, pk, submitted, *, using=DEFAULT_DB_ALIAS):
    """Raise PermissionsViolation when a caller without the activate action moves a source field."""
    if user is None or user.has_perm(ACTIVATE_PERMISSION):
        return
    if moved := moved_source_fields(pk, submitted, using=using):
        error = PermissionsViolation()
        # message is a class attribute, so a constructor argument would not reach a reader of it.
        error.message = _('Changing {fields} requires the Script Project activate permission.').format(
            fields=', '.join(moved)
        )
        raise error
