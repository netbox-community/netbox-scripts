from django import forms
from django.utils.translation import gettext_lazy as _

from core.choices import JobIntervalChoices, JobNotificationChoices
from utilities.datetime import local_now
from utilities.forms.widgets import DateTimePicker, NumberWithOptions

__all__ = ('ScriptForm',)


class ScriptForm(forms.Form):
    """
    Base form for executing a Script.

    The script's variable fields are added dynamically by BaseScript.as_form(). The two
    scheduling fields are dropped when the class declares scheduling_enabled = False, so a
    script its author considers unsafe to run unattended never offers them.
    """

    # Labels and help text are deliberately kept identical to the wording operators
    # already know from existing script runs
    _commit = forms.BooleanField(
        required=False,
        initial=True,
        label=_('Commit changes'),
        help_text=_('Commit changes to the database (uncheck for a dry-run)'),
    )
    _schedule_at = forms.DateTimeField(
        required=False,
        widget=DateTimePicker(),
        label=_('Schedule at'),
        help_text=_('Schedule execution of script to a set time'),
    )
    _interval = forms.IntegerField(
        required=False,
        min_value=1,
        widget=NumberWithOptions(options=JobIntervalChoices),
        label=_('Recurs every'),
        help_text=_('Interval at which this script is re-run (in minutes)'),
    )
    _notifications = forms.ChoiceField(
        required=False,
        choices=JobNotificationChoices,
        initial=JobNotificationChoices.NOTIFICATION_ALWAYS,
        label=_('Notifications'),
        help_text=_('When to notify the user of job completion'),
    )

    # Removed as a pair, because a recurrence is meaningless without a start time
    SCHEDULING_FIELDS = ('_schedule_at', '_interval')

    def __init__(self, *args, scheduling_enabled=True, notifications_default=None, **kwargs):
        super().__init__(*args, **kwargs)

        if notifications_default:
            self.fields['_notifications'].initial = notifications_default

        if not scheduling_enabled:
            for name in self.SCHEDULING_FIELDS:
                self.fields.pop(name)
            return

        # The server's clock is not necessarily the operator's, and a time typed against the
        # wrong one is rejected as being in the past with no clue why. Composed per instance
        # rather than on the field, whose help text is evaluated once at import.
        schedule = self.fields['_schedule_at']
        schedule.help_text = f'{schedule.help_text}{self.clock_hint()}'

    @staticmethod
    def clock_hint():
        """Return the parenthetical naming the server's current time, for the schedule field."""
        return _(' (current time: <strong>{now}</strong>)').format(now=local_now().strftime('%Y-%m-%d %H:%M:%S %Z'))

    def clean(self):
        """
        Refuse a schedule in the past, and settle the two values a submission can leave open.

        A recurrence with no start time is anchored to now, and an unsubmitted notification
        choice takes the policy the script class declared.
        """
        cleaned = super().clean()

        start = cleaned.get('_schedule_at')
        if start and start < local_now():
            # Scoped to the field, so the message renders beside the input the operator has to
            # correct rather than at the top of a form that may be long.
            raise forms.ValidationError({'_schedule_at': _('Scheduled time must be in the future.')})

        # Submitting an interval alone means "from now", which is the only reading that does
        # not silently discard the recurrence.
        if cleaned.get('_interval') and start is None:
            cleaned['_schedule_at'] = local_now()

        # The field is optional so that a run can be requested without naming a policy, which
        # then means the one the script class declared rather than an empty string.
        if not cleaned.get('_notifications'):
            cleaned['_notifications'] = self.fields['_notifications'].initial

        return cleaned
