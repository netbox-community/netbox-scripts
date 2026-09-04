"""
Guarantees this plugin gets from a base class rather than from its own code.

Each one is correct today because something the plugin inherits makes it so, and nothing else in
the suite pins any of them. An override added later for an unrelated reason would drop the
guarantee silently, and a reviewer reading our code would credit us for behaviour we did not
write. Each guarantee is asserted as behaviour first, so it survives core changing how the
guarantee is delivered, and then as the flag or class it rests on, which names the cause when the
behaviour test fails.
"""

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from rest_framework import status

from core.events import OBJECT_CREATED
from core.models import Job, ObjectType
from dcim.models import Device
from extras.events import EventContext, process_event_rules
from extras.models import EventRule
from netbox.event_rules import get_event_rule_action
from netbox_scripts.models import CustomScript
from netbox_scripts.tests.plugin_testing import PluginAPIViewTestCase
from netbox_scripts.tests.test_execution import MAKES_A_TAG, ScriptJobTestMixin
from netbox_scripts.tests.views.test_run import RunViewTestMixin
from utilities.testing import APITestCase

SLUG = 'netbox_scripts.run'


class ReadOnlyTokenTestCase(RunViewTestMixin, PluginAPIViewTestCase, APITestCase):
    """
    A read-only token cannot start a run.

    RunScriptPermissions overrides perms_map only, so the refusal comes from TokenPermissions,
    which every other write route here also inherits. Nothing else in the suite covers it.

    TokenPermissions delivers it from both has_permission and has_object_permission, and the run
    action is object-level, so dropping one still refuses. An override has to miss both to reach
    the endpoint.
    """

    model = CustomScript

    def setUp(self):
        super().setUp()
        # No queue worker runs under test, and the endpoint refuses a run nothing can pick up.
        self.enterContext(patch('netbox_scripts.api.views.any_workers_for_queue', return_value=True))
        self.add_permissions('netbox_scripts.run_customscript')

    def post_run(self):
        viewname = f'{self._get_view_namespace()}:customscript-run'
        url = reverse(viewname, kwargs={'pk': self.script.pk})
        return self.client.post(url, {'data': {'label': 'made-by-a-token'}}, format='json', **self.header)

    def runs(self):
        return Job.objects.filter(object_id=self.script.pk)

    def test_a_read_only_token_cannot_start_a_run(self):
        self.token.write_enabled = False
        self.token.save()

        response = self.post_run()

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        # DRF returns 403 for an authentication failure too, so the body is what separates a
        # refused permission from a token that stopped authenticating.
        self.assertNotIn('token', str(response.data).lower())
        self.assertFalse(self.runs().exists())

    def test_a_write_enabled_token_can(self):
        # The control: without it the refusal above would pass for any reason a run is refused.
        self.assertHttpStatus(self.post_run(), status.HTTP_201_CREATED)
        self.assertTrue(self.runs().exists())


class RaisingActionIsolationTestCase(ScriptJobTestMixin, TestCase):
    """
    One failing rule does not end the dispatch batch.

    The isolation comes from is_plugin_provided, which core defaults to True on the base class and
    again at registration. We never set it, so a future decision to register explicitly could
    switch it off without any test noticing.

    register_event_rule_action assigns the flag onto the instance, so a class attribute is
    overwritten and only the registered instance decides. That is what these assert against.
    """

    def setUp(self):
        super().setUp()
        self.publish({'deploy.py': MAKES_A_TAG})
        self.custom_script = self.script()
        self.object_type = ObjectType.objects.get_for_model(Device)
        self.rules = [self.rule('First'), self.rule('Second')]

    def rule(self, name):
        rule = EventRule.objects.create(
            name=name,
            event_types=[OBJECT_CREATED],
            action_type=SLUG,
            action_object=self.custom_script,
        )
        rule.object_types.set([ObjectType.objects.get_for_model(Device)])
        return rule

    def event(self):
        return EventContext(
            event_type=OBJECT_CREATED,
            data={'id': 7, 'name': 'device-1'},
            object_type=self.object_type,
            object_id=7,
            snapshots={'prechange': None, 'postchange': {'name': 'device-1'}},
            user=self.user,
        )

    def test_a_raising_action_neither_propagates_nor_stops_the_batch(self):
        action = get_event_rule_action(SLUG)
        calls = []
        original = type(action).enqueue

        def failing(self, *, event_rule, **kwargs):
            calls.append(event_rule.name)
            if event_rule.name == 'First':
                raise RuntimeError('the action blew up')
            return original(self, event_rule=event_rule, **kwargs)

        # patch.object, so a later move of enqueue to a base class still restores cleanly.
        # No assertRaises: the point is that this returns. The log assertion pins the other half,
        # that the failure is reported rather than swallowed.
        with (
            patch.object(type(action), 'enqueue', failing),
            self.assertLogs('netbox.events_processor', level='ERROR'),
        ):
            process_event_rules(self.rules, self.object_type, self.event())

        self.assertEqual(calls, ['First', 'Second'])
        self.assertEqual(Job.objects.filter(object_id=self.custom_script.pk).count(), 1)

    def test_the_action_is_registered_as_plugin_provided(self):
        # Behaviour first, then the flag the behaviour rests on, so switching it fails here too.
        self.assertTrue(get_event_rule_action(SLUG).is_plugin_provided)
