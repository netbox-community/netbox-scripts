import inspect
import logging

from django.utils import timezone
from django.utils.functional import classproperty
from django.utils.translation import gettext as _

from core.choices import JobNotificationChoices

from .forms import ScriptForm
from .logging import LogLevelChoices
from .variables import ScriptVariable

__all__ = (
    'BaseScript',
    'Script',
)


class BaseScript:
    """
    Foundation for Custom Scripts.

    Authors normally subclass ``Script``. Subclassing this class directly is for shared
    building blocks that must not show up as runnable scripts themselves.
    """

    # Keep Django templates from calling the class when they resolve it as a variable
    do_not_call_in_templates = True

    class Meta:
        pass

    def __init__(self):
        self.messages = []  # Structured records shown alongside the run result
        self.output = ''
        self.failed = False

        # Populated by the execution runner when the script runs inside a request
        self.request = None

        # Scripts in different projects may share a module and class name, which
        # collapses to one logger name here. The loader assigns a project-qualified
        # logger identity when project loading ships (tracked for the loader PR).
        self.logger = logging.getLogger(f'netbox.plugins.netbox_custom_scripts.scripts.{self.module}.{self.class_name}')

    def __str__(self):
        return self.name

    #
    # Identity
    #

    @classproperty
    def module(cls):
        """Return the logical module name this script is published under."""
        # The loader assigns _custom_script_module with the logical module name, so
        # generated runtime import namespaces never leak into user-facing identity.
        # Only the class's own dictionary is consulted. The marker records where a
        # discovered class was found, so subclasses must not inherit it.
        return cls.__dict__.get('_custom_script_module') or cls.__module__

    @classproperty
    def class_name(cls):
        """Return the script's own class name."""
        return cls.__name__

    @classproperty
    def full_name(cls):
        """Return the dotted module and class name identifying this script."""
        return f'{cls.module}.{cls.class_name}'

    @classmethod
    def root_module(cls):
        """Return the first segment of the script's module path."""
        return cls.module.split('.')[0]

    #
    # Author-defined Meta attributes
    #

    @classproperty
    def name(cls):
        """Return the display name from Meta, defaulting to the class name."""
        return getattr(cls.Meta, 'name', cls.__name__)

    @classproperty
    def description(cls):
        """Return the description from Meta, defaulting to an empty string."""
        return getattr(cls.Meta, 'description', '')

    @classproperty
    def field_order(cls):
        """Return the field order from Meta, or None to keep declaration order."""
        return getattr(cls.Meta, 'field_order', None)

    @classproperty
    def fieldsets(cls):
        """Return the fieldset layout from Meta, or None to build a default layout."""
        return getattr(cls.Meta, 'fieldsets', None)

    @classproperty
    def commit_default(cls):
        """Return whether the run form's commit toggle starts enabled."""
        return getattr(cls.Meta, 'commit_default', True)

    @classproperty
    def job_timeout(cls):
        """Return the job timeout from Meta, or None for the system default."""
        return getattr(cls.Meta, 'job_timeout', None)

    @classproperty
    def scheduling_enabled(cls):
        """Return whether this script may be scheduled."""
        return getattr(cls.Meta, 'scheduling_enabled', True)

    @classproperty
    def notifications_default(cls):
        """Return the default job notification policy from Meta."""
        return getattr(cls.Meta, 'notifications_default', JobNotificationChoices.NOTIFICATION_ALWAYS)

    #
    # Variables
    #

    @classmethod
    def _get_vars(cls):
        script_vars = {}

        # Walk the MRO most-derived class first. Within a class body source order is
        # kept, and the first definition of a name wins.
        for ancestor in inspect.getmro(cls):
            if ancestor is object:
                break

            for name, attr in ancestor.__dict__.items():
                if name not in script_vars and issubclass(attr.__class__, ScriptVariable):
                    script_vars[name] = attr

        # field_order pins the listed variables first, unlisted variables keep their order
        if not cls.field_order:
            return script_vars
        ordered_vars = {field: script_vars.pop(field) for field in cls.field_order if field in script_vars}
        ordered_vars.update(script_vars)

        return ordered_vars

    #
    # Execution
    #

    def run(self, data, commit):
        """Run the script. Authors must override this method."""
        raise NotImplementedError('A Custom Script must define a run(self, data, commit) method.')

    def get_job_data(self):
        """Bundle the run's log and output for storage on the executing Job."""
        return {
            'log': self.messages,
            'output': self.output,
        }

    #
    # Run form
    #

    def get_fieldsets(self):
        """Return the run form's fieldsets, either the author's layout or a default one."""
        fieldsets = []

        if self.fieldsets:
            fieldsets.extend(self.fieldsets)
        else:
            fields = list(self._get_vars())
            fieldsets.append((_('Script Data'), fields))

        # Scheduling and notifications are not implemented yet, so the execution group
        # holds only the commit toggle regardless of scheduling_enabled
        fieldsets.append((_('Script Execution Parameters'), ('_commit',)))

        return fieldsets

    def as_form(self, data=None, files=None, initial=None):
        """
        Construct the run form for this script.

        The form is a ``ScriptForm`` subclass carrying one field per variable plus the
        commit toggle.
        """
        fields = {name: var.as_field() for name, var in self._get_vars().items()}
        form_class = type('ScriptForm', (ScriptForm,), fields)

        form = form_class(data, files, initial=initial)
        form.fields['_commit'].initial = self.commit_default

        return form

    #
    # Logging
    #

    def _log(self, message, obj=None, level=LogLevelChoices.LOG_INFO):
        """
        Append one record to the run log and mirror it to the system logger.

        Script code should call the level-specific ``log_*`` helpers instead.
        """
        if level not in LogLevelChoices.values():
            raise ValueError(f'Invalid logging level: {level}')

        if message:
            self.messages.append(
                {
                    'time': timezone.now().isoformat(),
                    'status': level,
                    'message': str(message),
                    'obj': str(obj) if obj else None,
                    'url': obj.get_absolute_url() if hasattr(obj, 'get_absolute_url') else None,
                }
            )

            # The stdlib mirror has no obj field, so the object is folded into the text
            if obj:
                message = f'{obj}: {message}'
            self.logger.log(LogLevelChoices.SYSTEM_LEVELS[level], message)

    def log_debug(self, message=None, obj=None):
        """Record a debug-level message on the run log."""
        self._log(message, obj, level=LogLevelChoices.LOG_DEBUG)

    def log_success(self, message=None, obj=None):
        """Record a success-level message on the run log."""
        self._log(message, obj, level=LogLevelChoices.LOG_SUCCESS)

    def log_info(self, message=None, obj=None):
        """Record an info-level message on the run log."""
        self._log(message, obj, level=LogLevelChoices.LOG_INFO)

    def log_warning(self, message=None, obj=None):
        """Record a warning-level message on the run log."""
        self._log(message, obj, level=LogLevelChoices.LOG_WARNING)

    def log_failure(self, message=None, obj=None):
        """Record a failure-level message on the run log and mark the run failed."""
        self._log(message, obj, level=LogLevelChoices.LOG_FAILURE)
        self.failed = True


class Script(BaseScript):
    """
    Marker base class for runnable scripts.

    Script discovery (a later release) publishes subclasses of this class, while plain
    ``BaseScript`` subclasses stay private helpers.
    """
