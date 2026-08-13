import importlib.util
import unittest

import django_rq
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.test import TestCase, override_settings

from core.events import JOB_COMPLETED, OBJECT_CREATED
from core.models import Job, ObjectType
from dcim.models import Device
from extras.events import EventContext
from extras.models import EventRule
from netbox_custom_scripts.models import CustomScript
from netbox_custom_scripts.tests.test_event_sources import scratch_queues
from netbox_custom_scripts.tests.test_execution import MAKES_A_TAG, ScriptJobTestMixin

# The registry arrived in NetBox 4.7. Asking what the host provides mirrors compat._host_provides(),
# and keeps a version literal out of the suite.
HAS_EVENT_RULE_ACTIONS = importlib.util.find_spec('netbox.event_rules') is not None

if HAS_EVENT_RULE_ACTIONS:
    from netbox.event_rules import get_event_rule_action
    from netbox_custom_scripts.event_rules import RunCustomScriptAction

SLUG = 'netbox_custom_scripts.run'
REASON = 'This NetBox version has no Event Rule action registry.'


@unittest.skipUnless(HAS_EVENT_RULE_ACTIONS, REASON)
class RunCustomScriptActionTestCase(ScriptJobTestMixin, TestCase):
    """The registered action: registration, validation, dispatch, and its refusals."""

    def setUp(self):
        super().setUp()
        self.publish({'deploy.py': MAKES_A_TAG})
        self.custom_script = self.script()
        self.action = get_event_rule_action(SLUG)
        self.rule = EventRule.objects.create(
            name='On a device change',
            event_types=[OBJECT_CREATED],
            action_type=SLUG,
            action_object=self.custom_script,
        )
        self.rule.object_types.set([ObjectType.objects.get_for_model(Device)])

    def context(self, **overrides):
        """Build the event context core hands an action for a created Device."""
        values = {
            'event_type': OBJECT_CREATED,
            'data': {'id': 7, 'name': 'device-1'},
            'object_type': ObjectType.objects.get_for_model(Device),
            'object_id': 7,
            'snapshots': {'prechange': None, 'postchange': {'name': 'device-1'}},
            'user': self.user,
        }
        values.update(overrides)
        return EventContext(**values)

    def fire(self, action_data=None, context=None):
        """Dispatch the action the way core does."""
        self.action.enqueue(
            event_rule=self.rule,
            event_context=context or self.context(),
            action_object=self.custom_script,
            action_data=action_data if action_data is not None else {},
        )

    def runs(self):
        return Job.objects.filter(object_id=self.custom_script.pk)

    def test_the_action_is_registered(self):
        self.assertIsNotNone(self.action)
        self.assertIs(self.action.object_model, CustomScript)
        self.assertTrue(self.action.object_required)

    def test_the_registered_instance_is_ours(self):
        self.assertIsInstance(self.action, RunCustomScriptAction)

    def test_a_firing_rule_enqueues_exactly_one_run(self):
        # The acceptance criterion is one run rather than zero or two.
        self.fire(action_data={'name': 'device-1'})

        self.assertEqual(self.runs().count(), 1)
        self.assertEqual(self.runs().first().data['module_path'], self.custom_script.module_path)

    def test_the_event_context_reaches_the_run(self):
        self.fire()

        event = self.runs().get().data['event']
        self.assertEqual(event['event_type'], OBJECT_CREATED)
        self.assertEqual(event['object_type'], 'dcim.device')
        self.assertEqual(event['object_id'], 7)
        self.assertEqual(event['event_rule_id'], self.rule.pk)
        # Neither is JSON-safe, and both already travel by their own route.
        self.assertNotIn('request', event)
        self.assertNotIn('user', event)

    def test_a_disabled_script_is_reported_rather_than_queued(self):
        self.custom_script.enabled = False
        self.custom_script.save()

        with self.assertLogs('netbox.plugins.netbox_custom_scripts.event_rules', level='ERROR'):
            self.fire()

        self.assertFalse(self.runs().exists())

    def test_a_project_serving_nothing_is_reported_rather_than_queued(self):
        self.project.active_revision = None
        self.project.save()

        with self.assertLogs('netbox.plugins.netbox_custom_scripts.event_rules', level='ERROR'):
            self.fire()

        self.assertFalse(self.runs().exists())

    def test_a_retired_script_cannot_be_selected(self):
        # _validate() is the entry point EventRule.clean() uses, so it is what the test drives.
        self.custom_script.is_retired = True
        self.custom_script.save()

        with self.assertRaises(ValidationError):
            self.action._validate(action_object=self.custom_script, action_data={})

    def test_a_runnable_script_validates(self):
        self.assertIsNone(self.action._validate(action_object=self.custom_script, action_data={}))

    def test_a_disabled_script_still_validates(self):
        # Disabling is temporary state an administrator flips back, so it is caught at dispatch.
        self.custom_script.enabled = False
        self.custom_script.save()

        self.assertIsNone(self.action._validate(action_object=self.custom_script, action_data={}))

    def test_the_action_requires_a_target_object(self):
        with self.assertRaises(ValidationError):
            self.action._validate(action_object=None, action_data={})

    def test_a_job_event_without_a_request_still_dispatches(self):
        # Job lifecycle events carry no request, and the handler must not assume one.
        context = self.context(event_type=JOB_COMPLETED, object_type=None, object_id=None)

        self.fire(context=context)

        self.assertEqual(self.runs().count(), 1)
        self.assertIsNone(self.runs().get().data['event']['object_type'])

    def test_an_import_value_resolves_to_a_script(self):
        value = f'{self.custom_script.project.key}:{self.custom_script.full_name}'

        self.assertEqual(self.action.resolve_import_object(value), self.custom_script)

    def test_an_unresolvable_import_value_raises(self):
        with self.assertRaises(ObjectDoesNotExist):
            self.action.resolve_import_object('nope:nope.Nope')

    def test_a_script_in_another_project_does_not_resolve(self):
        # full_name alone is not unique across projects, which is why the key is part of the value.
        with self.assertRaises(ObjectDoesNotExist):
            self.action.resolve_import_object(f'other:{self.custom_script.full_name}')


@unittest.skipUnless(HAS_EVENT_RULE_ACTIONS, REASON)
@override_settings(RQ_QUEUES=scratch_queues())
class ActionInputTestCase(ScriptJobTestMixin, TestCase):
    """That the rule's action_data reaches the script as its input, verbatim."""

    # enqueue_run keeps input off the Job row on purpose, because a variable resolves to a model
    # instance or an uploaded file. So the queued task is the only place it can be read, which is
    # why this class needs a real queue while the rest of the suite does not.

    def setUp(self):
        super().setUp()
        # Only this queue's own keys are removed, and it is on a scratch database, because a
        # developer's Redis is shared with a running NetBox.
        self.queue = django_rq.get_queue('default')
        self.queue.empty()
        self.addCleanup(self.queue.empty)
        self.publish({'deploy.py': MAKES_A_TAG})
        self.custom_script = self.script()
        self.action = get_event_rule_action(SLUG)
        self.rule = EventRule.objects.create(
            name='On a device change',
            event_types=[OBJECT_CREATED],
            action_type=SLUG,
            action_object=self.custom_script,
        )

    def test_the_action_data_becomes_the_script_input(self):
        # django_rq defers an enqueue to transaction.on_commit, and a TestCase never commits.
        with self.captureOnCommitCallbacks(execute=True):
            self.action.enqueue(
                event_rule=self.rule,
                event_context=EventContext(event_type=OBJECT_CREATED, data={}, user=self.user),
                action_object=self.custom_script,
                action_data={'name': 'device-1', 'count': 2},
            )

        self.assertEqual(self.queue.count, 1)
        self.assertEqual(self.queue.jobs[0].kwargs['data'], {'name': 'device-1', 'count': 2})
