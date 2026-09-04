from django.utils.translation import gettext_lazy as _
from rest_framework import serializers

from core.choices import JobNotificationChoices
from utilities.datetime import local_now


class CustomScriptRunInputSerializer(serializers.Serializer):
    """
    One run request: the script's variable values, plus the parameters the run itself takes.

    Values nest under data, so a variable named commit or interval cannot collide with an
    execution parameter. What is inside data is validated by the script class's own form.
    """

    # All optional, so a body written against the built-in run endpoint still validates here.
    data = serializers.JSONField(required=False, default=dict)
    commit = serializers.BooleanField(required=False)
    schedule_at = serializers.DateTimeField(required=False, allow_null=True)
    interval = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    notifications = serializers.ChoiceField(choices=JobNotificationChoices, required=False, allow_null=True)

    def validate_data(self, value):
        """Refuse anything but an object, since the values are handed to a form as a mapping."""
        if not isinstance(value, dict):
            raise serializers.ValidationError(_('Provide the variable values as an object.'))
        return value

    def validate_schedule_at(self, value):
        """Refuse a time already past, and one the script class forbids scheduling at all."""
        if value and value < local_now():
            raise serializers.ValidationError(_('Scheduled time must be in the future.'))
        return self._require_scheduling(value)

    def validate_interval(self, value):
        """Refuse a recurrence for a script whose author disallowed unattended runs."""
        return self._require_scheduling(value)

    def _require_scheduling(self, value):
        # The run form honours the author's flag by omitting the fields, which is presentation.
        # A caller that never saw a form has to be refused instead.
        if value and not self.context['script_class'].scheduling_enabled:
            raise serializers.ValidationError(_('Scheduling is not enabled for this Custom Script.'))
        return value

    def validate(self, data):
        """Anchor a recurrence that named no start time, the same rule the run form applies."""
        if data.get('interval') and not data.get('schedule_at'):
            data['schedule_at'] = local_now()
        return super().validate(data)
