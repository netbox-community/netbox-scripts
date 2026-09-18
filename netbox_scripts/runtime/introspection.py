"""
Description of published script classes for storage on a revision.

Discovery says which classes a revision publishes. This module says what each one is, and
proves the claim by building the run form before recording anything. A variable stores only
its keyword arguments at class-definition time, so the field it wants is built later, and a
combination Django rejects is invisible until something asks for the form. Asking here means
a fault of that kind reaches a verdict instead of the first execution attempt.

The result is a JSON-safe snapshot the revision keeps, and the build and validate pair works
like the manifest and the script file snapshot: whatever comes back out of the database is
checked before use. It carries no digest, unlike those two, because activation re-derives
rows from it rather than executing it.
"""

from django.utils.translation import gettext_lazy as _
from rq.exceptions import TimeoutFormatError
from rq.utils import parse_timeout

from core.choices import JobNotificationChoices

from ..constants import (
    MAX_SCRIPT_CLASS_NAME_LENGTH,
    MAX_SCRIPT_DISPLAY_NAME_LENGTH,
    MAX_SCRIPT_MODULE_PATH_LENGTH,
)
from ..scripts.forms import ScriptForm
from .exceptions import ScriptMetadataError

__all__ = (
    'RESERVED_VARIABLE_NAMES',
    'describe_script',
    'validate_discovered_scripts',
    'validate_job_timeout',
    'validate_notification_policy',
)

# Variable names the run form already owns. as_form() folds the variables into a ScriptForm
# subclass, so a variable sharing one of these names replaces the base field silently. The set
# is derived rather than spelled out, so a field added to the run form is covered on arrival.
RESERVED_VARIABLE_NAMES = frozenset(ScriptForm.declared_fields)

_REQUIRED_TEXT_KEYS = ('module_path', 'class_name', 'script_file_path', 'display_name', 'description')


def describe_script(discovered, *, script_file_id, script_file_path, position, passthrough=()):
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
    display_name = str(cls.name)
    _require_storable_display_name(display_name, discovered.name)

    try:
        instance = cls()
        form = instance.as_form()
        _require_known_fieldset_fields(instance, form)
    except ScriptMetadataError:
        raise
    except Exception as error:
        if isinstance(error, passthrough):
            raise
        raise ScriptMetadataError(
            _('The run form of "{name}" could not be built.').format(name=discovered.name),
            code='form_construction_failed',
            name=discovered.name,
        ) from error

    return {
        'module_path': discovered.logical_module,
        'class_name': discovered.name,
        'script_file_id': script_file_id,
        'script_file_path': script_file_path,
        'position': position,
        'display_name': display_name,
        'description': str(cls.description),
        'metadata': {
            'commit_default': bool(cls.commit_default),
            'scheduling_enabled': bool(cls.scheduling_enabled),
            'job_timeout': _job_timeout(cls),
            'notifications_default': validate_notification_policy(cls.notifications_default, name=cls.__name__),
        },
    }


def validate_discovered_scripts(value):
    """
    Return one stored discovery snapshot, refusing anything a build could not have produced.

    Validation is the only writer, so a value failing here means the row was changed outside
    that path. Raises ScriptMetadataError.
    """
    if not isinstance(value, list):
        raise ScriptMetadataError(_('The discovered scripts must be a list.'), code='invalid_snapshot')

    identities = set()
    for index, record in enumerate(value):
        if not isinstance(record, dict):
            raise ScriptMetadataError(
                _('Entry {index} of the discovered scripts must be an object.').format(index=index),
                code='invalid_entry',
            )
        for key in _REQUIRED_TEXT_KEYS:
            if not isinstance(record.get(key), str):
                raise ScriptMetadataError(
                    _('Entry {index} of the discovered scripts is missing a text "{key}".').format(
                        index=index, key=key
                    ),
                    code='invalid_entry',
                    name=key,
                )
        script_file_id = record.get('script_file_id')
        # bool is a subclass of int, so True would otherwise pass as a module id.
        if not isinstance(script_file_id, int) or isinstance(script_file_id, bool) or script_file_id <= 0:
            raise ScriptMetadataError(
                _('Entry {index} of the discovered scripts is missing a script file id.').format(index=index),
                code='invalid_entry',
                name='script_file_id',
            )
        if record.get('position') != index:
            raise ScriptMetadataError(
                _('Entry {index} of the discovered scripts records the wrong position.').format(index=index),
                code='invalid_position',
            )
        if not isinstance(record.get('metadata'), dict):
            raise ScriptMetadataError(
                _('Entry {index} of the discovered scripts must carry an object of metadata.').format(index=index),
                code='invalid_entry',
                name='metadata',
            )
        metadata = record['metadata']
        # Discovery writes both, an older snapshot carries neither, and None means the system default.
        if metadata.get('job_timeout') is not None:
            validate_job_timeout(metadata['job_timeout'], name=record['class_name'])
        if metadata.get('notifications_default') is not None:
            validate_notification_policy(metadata['notifications_default'], name=record['class_name'])
        _require_storable_identity(record['module_path'], record['class_name'])
        _require_storable_display_name(record['display_name'], record['class_name'])
        identity = (record['module_path'], record['class_name'])
        if identity in identities:
            raise ScriptMetadataError(
                _('Two entries of the discovered scripts publish as "{module_path}.{class_name}".').format(
                    module_path=record['module_path'], class_name=record['class_name']
                ),
                code='duplicate_identity',
                name=record['class_name'],
            )
        identities.add(identity)

    return value


def validate_job_timeout(value, *, name=None):
    """Return a job timeout that is a positive number of seconds. Raises ScriptMetadataError for anything else."""
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        if name:
            message = _('The job timeout of "{name}" must be a positive number of seconds.').format(name=name)
        else:
            message = _('The recorded job timeout must be a positive number of seconds.')
        raise ScriptMetadataError(message, code='invalid_job_timeout', name=name)
    return value


def validate_notification_policy(value, *, name=None):
    """Return a notification policy that is one of the Job choices. Raises ScriptMetadataError for anything else."""
    if value not in JobNotificationChoices.values():
        if name:
            message = _('The notification policy of "{name}" is not a supported Job notification choice.').format(
                name=name
            )
        else:
            message = _('The recorded notification policy is not a supported Job notification choice.')
        raise ScriptMetadataError(message, code='invalid_notifications_default', name=name)
    return str(value)


def _job_timeout(cls):
    """Return a positive timeout using RQ's duration grammar, or None for the queue default."""
    if cls.job_timeout is None:
        return None
    try:
        return validate_job_timeout(parse_timeout(cls.job_timeout))
    except (TimeoutFormatError, TypeError, ValueError, AssertionError, ScriptMetadataError) as error:
        raise ScriptMetadataError(
            _('The job timeout of "{name}" must be a positive number of seconds or an RQ duration string.').format(
                name=cls.__name__
            ),
            code='invalid_job_timeout',
            name=cls.__name__,
        ) from error


def _require_free_variable_names(cls):
    """Refuse a class whose variables would replace a field the run form already owns."""
    reserved = sorted(RESERVED_VARIABLE_NAMES.intersection(cls._get_vars()))
    if reserved:
        raise ScriptMetadataError(
            _('The variable "{variable}" of "{name}" uses a name the run form reserves.').format(
                variable=reserved[0], name=cls.__name__
            ),
            code='reserved_variable_name',
            name=reserved[0],
        )


def _require_storable_identity(module_path, class_name):
    """Refuse an identity longer than the fields a published script is recorded in."""
    if len(class_name) > MAX_SCRIPT_CLASS_NAME_LENGTH:
        raise ScriptMetadataError(
            _('The class name "{name}" is too long to store.').format(name=class_name),
            code='identity_too_long',
            name=class_name,
        )
    if len(module_path) > MAX_SCRIPT_MODULE_PATH_LENGTH:
        raise ScriptMetadataError(
            _('The module path of "{name}" is too long to store.').format(name=class_name),
            code='identity_too_long',
            name=class_name,
        )


def _require_storable_display_name(display_name, class_name):
    """Refuse a display name longer than the field a published script records it in."""
    # Meta.name has no length bound of its own, so this is the only thing standing between an
    # over-long one and a database error at activation.
    if len(display_name) > MAX_SCRIPT_DISPLAY_NAME_LENGTH:
        raise ScriptMetadataError(
            _('The display name of "{name}" is too long to store.').format(name=class_name),
            code='display_name_too_long',
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
                    _('The fieldsets of "{type_name}" name "{field_name}", which is not a variable.').format(
                        type_name=type(instance).__name__, field_name=name
                    ),
                    code='unknown_fieldset_field',
                    name=name,
                )
