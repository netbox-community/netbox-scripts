"""
Description of published script classes for storage on a revision.

Discovery says which classes a revision publishes. This module says what each one is, and
proves the claim by building the run form before recording anything. A variable stores only
its keyword arguments at class-definition time, so the field it wants is built later, and a
combination Django rejects is invisible until something asks for the form. Asking here means
a fault of that kind reaches a verdict instead of the first execution attempt.

The result is a JSON-safe snapshot the revision keeps, and the build and validate pair works
like the manifest and the entrypoint snapshot: whatever comes back out of the database is
checked before use. It carries no digest, unlike those two, because activation re-derives
rows from it rather than executing it.
"""

from ..constants import MAX_SCRIPT_CLASS_NAME_LENGTH, MAX_SCRIPT_MODULE_PATH_LENGTH
from ..scripts.forms import ScriptForm
from .exceptions import ScriptMetadataError

__all__ = (
    'RESERVED_VARIABLE_NAMES',
    'describe_script',
    'validate_discovered_scripts',
)

# Variable names the run form already owns. as_form() folds the variables into a ScriptForm
# subclass, so a variable sharing one of these names replaces the base field silently. The set
# is derived rather than spelled out, so a field added to the run form is covered on arrival.
RESERVED_VARIABLE_NAMES = frozenset(ScriptForm.declared_fields)

_REQUIRED_TEXT_KEYS = ('module_path', 'class_name', 'entrypoint_path', 'display_name', 'description')


def describe_script(discovered, *, entrypoint_module_id, entrypoint_path, position):
    """
    Describe one published script class as a JSON-safe record.

    Builds the run form and resolves its fieldsets, so a variable Django cannot turn into a
    field, a variable shadowing a run-form field, and a fieldset naming something that does
    not exist are all reported here. Raises ScriptMetadataError for every such fault, and for
    an identity too long to store.
    """
    cls = discovered.cls
    _require_free_variable_names(cls)
    _require_storable_identity(discovered.logical_module, discovered.name)

    try:
        instance = cls()
        form = instance.as_form()
        _require_known_fieldset_fields(instance, form)
    except ScriptMetadataError:
        raise
    except Exception as error:
        raise ScriptMetadataError(
            f'The run form of "{discovered.name}" could not be built.',
            code='form_construction_failed',
            name=discovered.name,
        ) from error

    return {
        'module_path': discovered.logical_module,
        'class_name': discovered.name,
        'entrypoint_module_id': entrypoint_module_id,
        'entrypoint_path': entrypoint_path,
        'position': position,
        'display_name': str(cls.name),
        'description': str(cls.description),
        'metadata': {
            'commit_default': bool(cls.commit_default),
            'scheduling_enabled': bool(cls.scheduling_enabled),
            'job_timeout': _job_timeout(cls),
            'notifications_default': str(cls.notifications_default),
        },
    }


def validate_discovered_scripts(value):
    """
    Return one stored discovery snapshot, refusing anything a build could not have produced.

    Validation is the only writer, so a value failing here means the row was changed outside
    that path. Raises ScriptMetadataError.
    """
    if not isinstance(value, list):
        raise ScriptMetadataError('The discovered scripts must be a list.', code='invalid_snapshot')

    identities = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise ScriptMetadataError(
                f'Entry {index} of the discovered scripts must be an object.',
                code='invalid_entry',
            )
        for key in _REQUIRED_TEXT_KEYS:
            if not isinstance(record.get(key), str):
                raise ScriptMetadataError(
                    f'Entry {index} of the discovered scripts is missing a text "{key}".',
                    code='invalid_entry',
                    name=key,
                )
        if record.get('position') != index:
            raise ScriptMetadataError(
                f'Entry {index} of the discovered scripts records the wrong position.',
                code='invalid_position',
            )
        if not isinstance(record.get('metadata'), dict):
            raise ScriptMetadataError(
                f'Entry {index} of the discovered scripts must carry an object of metadata.',
                code='invalid_entry',
                name='metadata',
            )
        _require_storable_identity(record['module_path'], record['class_name'])
        identity = (record['module_path'], record['class_name'])
        if identity in identities:
            raise ScriptMetadataError(
                f'Two entries of the discovered scripts publish as "{record["module_path"]}.{record["class_name"]}".',
                code='duplicate_identity',
                name=record['class_name'],
            )
        identities.add(identity)

    return value


def _job_timeout(cls):
    """Return one class's job timeout as a whole number of seconds, or None for the default."""
    # The only Meta value that is neither text nor a flag, so it is the only one a str() or
    # bool() coercion would not already have made storable.
    if cls.job_timeout is None:
        return None
    try:
        return int(cls.job_timeout)
    except (TypeError, ValueError) as error:
        raise ScriptMetadataError(
            f'The job timeout of "{cls.__name__}" is not a number of seconds.',
            code='invalid_job_timeout',
            name=cls.__name__,
        ) from error


def _require_free_variable_names(cls):
    """Refuse a class whose variables would replace a field the run form already owns."""
    reserved = sorted(RESERVED_VARIABLE_NAMES.intersection(cls._get_vars()))
    if reserved:
        raise ScriptMetadataError(
            f'The variable "{reserved[0]}" of "{cls.__name__}" uses a name the run form reserves.',
            code='reserved_variable_name',
            name=reserved[0],
        )


def _require_storable_identity(module_path, class_name):
    """Refuse an identity longer than the fields a published script is recorded in."""
    if len(class_name) > MAX_SCRIPT_CLASS_NAME_LENGTH:
        raise ScriptMetadataError(
            f'The class name "{class_name}" is too long to store.',
            code='identity_too_long',
            name=class_name,
        )
    if len(module_path) > MAX_SCRIPT_MODULE_PATH_LENGTH:
        raise ScriptMetadataError(
            f'The module path of "{class_name}" is too long to store.',
            code='identity_too_long',
            name=class_name,
        )


def _require_known_fieldset_fields(instance, form):
    """Refuse a fieldset layout naming a field the built form does not have."""
    # field_order tolerates an unknown name, get_fieldsets() does not filter, so an unknown
    # name here reaches the template and breaks rendering rather than the build.
    for _label, names in instance.get_fieldsets():
        for name in names:
            if name not in form.fields:
                raise ScriptMetadataError(
                    f'The fieldsets of "{type(instance).__name__}" name "{name}", which is not a variable.',
                    code='unknown_fieldset_field',
                    name=name,
                )
