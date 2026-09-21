import django_rq
from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.test import TestCase

from core.events import JOB_COMPLETED, OBJECT_CREATED
from core.models import Job, ObjectType
from dcim.models import Device
from extras.events import EventContext
from extras.models import EventRule
from netbox.event_rules import get_event_rule_action
from netbox_scripts.event_rules import RunNetBoxScriptAction
from netbox_scripts.models import NetBoxScript
from netbox_scripts.tests.test_execution import MAKES_A_TAG, ScriptJobTestMixin

SLUG = 'netbox_scripts.run'


class RunNetBoxScriptActionTestCase(ScriptJobTestMixin, TestCase):
    """The registered action: registration, validation, dispatch, and its refusals."""

    def setUp(self):
        super().setUp()
        self.publish({'deploy.py': MAKES_A_TAG})
        self.netbox_script = self.script()
        self.action = get_event_rule_action(SLUG)
        self.rule = EventRule.objects.create(
            name='On a device change',
            event_types=[OBJECT_CREATED],
            action_type=SLUG,
            action_object=self.netbox_script,
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
            action_object=self.netbox_script,
            action_data=action_data if action_data is not None else {},
        )

    def runs(self):
        return Job.objects.filter(object_id=self.netbox_script.pk)

    def test_the_action_is_registered(self):
        self.assertIsNotNone(self.action)
        self.assertIs(self.action.object_model, NetBoxScript)
        self.assertTrue(self.action.object_required)

    def test_the_registered_instance_is_ours(self):
        self.assertIsInstance(self.action, RunNetBoxScriptAction)

    def test_a_firing_rule_enqueues_exactly_one_run(self):
        # The acceptance criterion is one run rather than zero or two.
        self.fire(action_data={'name': 'device-1'})

        self.assertEqual(self.runs().count(), 1)
        self.assertEqual(self.runs().first().data['module_path'], self.netbox_script.module_path)

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

    def test_the_snapshots_stay_off_the_queued_job_row(self):
        self.fire()

        event = self.runs().get().data['event']
        self.assertNotIn('snapshots', event)
        self.assertEqual(event['event_type'], OBJECT_CREATED)

    def test_the_snapshots_stay_off_an_immediate_job_row(self):
        # An immediate run builds its row in _run_now, by a different statement. It needs a payload
        # that still carries snapshots, which enqueue()'s own output no longer does.
        event = {
            'event_rule_id': self.rule.pk,
            'event_rule': str(self.rule),
            'event_type': OBJECT_CREATED,
            'object_type': 'dcim.device',
            'object_id': 7,
            'snapshots': {'prechange': None, 'postchange': {'name': 'device-1'}},
        }

        job = self.run_job(self.netbox_script, event=event)

        self.assertNotIn('snapshots', job.data['event'])
        self.assertEqual(job.data['event']['event_type'], OBJECT_CREATED)

    def test_a_disabled_script_is_reported_rather_than_queued(self):
        self.netbox_script.enabled = False
        self.netbox_script.save()

        with self.assertLogs('netbox.plugins.netbox_scripts.event_rules', level='ERROR'):
            self.fire()

        self.assertFalse(self.runs().exists())

    def test_a_project_serving_nothing_is_reported_rather_than_queued(self):
        self.project.active_revision = None
        self.project.save(update_fields=('active_revision',))

        with self.assertLogs('netbox.plugins.netbox_scripts.event_rules', level='ERROR'):
            self.fire()

        self.assertFalse(self.runs().exists())

    def test_a_malformed_recorded_setting_is_reported_rather_than_queued(self):
        NetBoxScript.objects.filter(pk=self.netbox_script.pk).update(
            metadata={**self.netbox_script.metadata, 'job_timeout': 'not-a-duration'}
        )
        self.netbox_script.refresh_from_db()

        with self.assertLogs('netbox.plugins.netbox_scripts.event_rules', level='ERROR') as logs:
            self.fire()

        self.assertIn('execution settings', logs.output[0])
        self.assertFalse(self.runs().exists())

    def test_a_retired_script_cannot_be_selected(self):
        # _validate() is the entry point EventRule.clean() uses, so it is what the test drives.
        self.netbox_script.is_retired = True
        self.netbox_script.save(update_fields=('is_retired',))

        with self.assertRaises(ValidationError):
            self.action._validate(action_object=self.netbox_script, action_data={})

    def test_a_runnable_script_validates(self):
        self.assertIsNone(self.action._validate(action_object=self.netbox_script, action_data={}))

    def test_a_disabled_script_still_validates(self):
        # Disabling is temporary state an administrator flips back, so it is caught at dispatch.
        self.netbox_script.enabled = False
        self.netbox_script.save()

        self.assertIsNone(self.action._validate(action_object=self.netbox_script, action_data={}))

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
        value = f'{self.netbox_script.project.key}:{self.netbox_script.full_name}'

        self.assertEqual(self.action.resolve_import_object(value), self.netbox_script)

    def test_an_unresolvable_import_value_raises(self):
        with self.assertRaises(ObjectDoesNotExist):
            self.action.resolve_import_object('nope:nope.Nope')

    def test_a_script_in_another_project_does_not_resolve(self):
        # full_name alone is not unique across projects, which is why the key is part of the value.
        with self.assertRaises(ObjectDoesNotExist):
            self.action.resolve_import_object(f'other:{self.netbox_script.full_name}')


class ActionInputTestCase(ScriptJobTestMixin, TestCase):
    """That the rule's action_data reaches the script as its input, verbatim."""

    # enqueue_run keeps input off the Job row on purpose, because a variable resolves to a model
    # instance or an uploaded file. So the queued task is the only place it can be read.

    def setUp(self):
        super().setUp()
        # The queue is isolated by testing/configuration.py, so this only separates one test from
        # the next. Never clear with NetBox's RQQueueTestMixin, which uses a server-wide flushall().
        self.queue = django_rq.get_queue('default')
        self.queue.empty()
        self.addCleanup(self.queue.empty)
        self.publish({'deploy.py': MAKES_A_TAG})
        self.netbox_script = self.script()
        self.action = get_event_rule_action(SLUG)
        self.rule = EventRule.objects.create(
            name='On a device change',
            event_types=[OBJECT_CREATED],
            action_type=SLUG,
            action_object=self.netbox_script,
        )

    def test_the_worker_still_receives_the_snapshots(self):
        # Filtering the row must not filter the task kwargs, which is how event reaches the script.
        context = EventContext(
            event_type=OBJECT_CREATED,
            data={},
            user=self.user,
            snapshots={'prechange': None, 'postchange': {'name': 'device-1'}},
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.action.enqueue(
                event_rule=self.rule,
                event_context=context,
                action_object=self.netbox_script,
                action_data={},
            )

        snapshots = self.queue.jobs[0].kwargs['event']['snapshots']
        self.assertEqual(snapshots['postchange'], {'name': 'device-1'})

    def test_the_action_data_becomes_the_script_input(self):
        # django_rq defers an enqueue to transaction.on_commit, and a TestCase never commits.
        with self.captureOnCommitCallbacks(execute=True):
            self.action.enqueue(
                event_rule=self.rule,
                event_context=EventContext(event_type=OBJECT_CREATED, data={}, user=self.user),
                action_object=self.netbox_script,
                action_data={'name': 'device-1', 'count': 2},
            )

        self.assertEqual(self.queue.count, 1)
        self.assertEqual(self.queue.jobs[0].kwargs['data'], {'name': 'device-1', 'count': 2})
