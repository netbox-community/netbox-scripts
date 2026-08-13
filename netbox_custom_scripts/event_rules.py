"""The Event Rule action that runs a Custom Script."""

import logging

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

from netbox.event_rules import EventRuleAction
from utilities.request import copy_safe_request

from .execution import ScriptNotExecutableError
from .jobs import CustomScriptJob
from .models import CustomScript

__all__ = (
    'RunCustomScriptAction',
    'event_rule_actions',
)

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.event_rules')


class RunCustomScriptAction(EventRuleAction):
    """Runs the selected Custom Script when an Event Rule fires."""

    slug = 'netbox_custom_scripts.run'
    label = _('Run Custom Script')
    description = _('Run a Custom Script published by an activated project revision')
    object_model = CustomScript
    object_required = True

    def validate(self, *, action_object, action_data):
        """Refuse a Custom Script that can never run again."""
        # Retirement is permanent, because the active revision has stopped publishing the class.
        # A disabled script or project is temporary state an administrator flips back, so those
        # are reported at dispatch instead of blocking the rule from being saved.
        if action_object.is_retired:
            raise ValidationError({'action_object_id': _('This Custom Script is retired and cannot be run.')})

    def enqueue(self, *, event_rule, event_context, action_object, action_data):
        """Queue one run of the Custom Script, reporting a script that cannot run."""
        request = event_context.get('request')
        try:
            CustomScriptJob.enqueue_run(
                action_object,
                data=action_data,
                # An event-driven run is an automation, and a dry run would make the rule a no-op
                # with no way to say so.
                commit=True,
                # The worker is another process, so the request has to be picklable and stripped
                # of anything sensitive before it travels.
                request=copy_safe_request(request, include_files=False) if request else None,
                user=event_context.get('user'),
                event=self._event_payload(event_rule, event_context),
            )
        except ScriptNotExecutableError as error:
            # One rule's misconfiguration must not end the batch, so this is reported and dropped.
            logger.error(f'Event rule "{event_rule}" could not run {action_object}: {error}')

    def resolve_import_object(self, value):
        """Resolve "<project key>:<module path>.<class name>" to one Custom Script."""
        # The dotted name alone is not unique, because two projects may publish the same one.
        project_key, _separator, full_name = value.partition(':')
        module_path, _dot, class_name = full_name.rpartition('.')
        return CustomScript.objects.get(project__key=project_key, module_path=module_path, class_name=class_name)

    @staticmethod
    def _event_payload(event_rule, event_context):
        """Return the JSON-safe part of the event context, since it rides on the Job row."""
        object_type = event_context.get('object_type')
        return {
            'event_rule_id': event_rule.pk,
            'event_rule': str(event_rule),
            'event_type': event_context.get('event_type'),
            'object_type': f'{object_type.app_label}.{object_type.model}' if object_type else None,
            'object_id': event_context.get('object_id'),
            'snapshots': event_context.get('snapshots'),
        }


event_rule_actions = [RunCustomScriptAction]
