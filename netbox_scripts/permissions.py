"""Object-level authorization for parent-driven Script File writes."""

from utilities.exceptions import PermissionsViolation


def validate_script_file_permissions(user, *, created=(), changed=(), using):
    """Refuse rows outside the user's add or change scope, inside the caller's transaction."""
    from .models import ScriptFile

    if user is None:
        return
    for action, keys in (('add', created), ('change', changed)):
        keys = set(keys)
        if keys and ScriptFile.objects.using(using).restrict(user, action).filter(pk__in=keys).count() != len(keys):
            raise PermissionsViolation()
