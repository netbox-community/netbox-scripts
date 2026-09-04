import django_rq
from django.test import TestCase

from core.events import OBJECT_CREATED
from core.models import ObjectType
from extras.choices import EventRuleActionChoices
from extras.events import process_event_rules, serialize_for_event
from extras.models import EventRule, Webhook
from netbox.models.features import has_feature
from netbox_scripts.models import (
    CustomScript,
    CustomScriptModule,
    ScriptProject,
    ScriptProjectRevision,
)

EVERY_MODEL = (ScriptProject, ScriptProjectRevision, CustomScriptModule, CustomScript)


class EventSourceFeatureTestCase(TestCase):
    """Whether the plugin's own models can drive an Event Rule."""

    def test_every_model_reports_the_event_rules_feature(self):
        # The registry holds a qualifying function per feature, so this asserts the registration
        # rather than the base class.
        for model in EVERY_MODEL:
            with self.subTest(model=model.__name__):
                self.assertTrue(has_feature(model, 'event_rules'))

    def test_a_rule_can_be_saved_against_a_plugin_model(self):
        object_type = ObjectType.objects.get_for_model(ScriptProject)
        webhook = Webhook.objects.create(name='Receiver', payload_url='http://localhost/hook/')
        rule = EventRule(
            name='On a new project',
            event_types=[OBJECT_CREATED],
            action_type=EventRuleActionChoices.WEBHOOK,
            action_object=webhook,
        )
        rule.full_clean()
        rule.save()
        rule.object_types.set([object_type])

        # object_types relates to ContentType, so identity is compared by primary key.
        self.assertEqual([ct.pk for ct in rule.object_types.all()], [object_type.pk])


class EventBodyTestCase(TestCase):
    """The payload a webhook would carry for each of the plugin's models."""

    def setUp(self):
        super().setUp()
        self.project = ScriptProject.objects.create(name='Serialized', key='serialized')

    def test_the_body_carries_the_project_identity(self):
        body = serialize_for_event(self.project)

        self.assertEqual(body['id'], self.project.pk)
        self.assertEqual(body['key'], 'serialized')
        # A webhook body goes through the same serializer, so a choice field carries the pair
        # here too, matching every NetBox event body.
        self.assertEqual(body['source_type'], {'value': self.project.source_type, 'label': 'Upload'})

    def test_the_body_omits_a_revision_stored_document(self):
        revision = ScriptProjectRevision.objects.create(
            project=self.project, digest='a' * 64, manifest={'files': {'deploy.py': {'sha256': 'b' * 64}}}
        )

        body = serialize_for_event(revision)

        self.assertEqual(body['digest'], 'a' * 64)
        # The manifest and the entrypoint snapshot are the revision's stored documents, and a
        # webhook body travels to whatever is on the other end.
        self.assertNotIn('manifest', body)
        self.assertNotIn('entrypoints', body)

    def test_every_model_serializes(self):
        revision = ScriptProjectRevision.objects.create(project=self.project, digest='c' * 64)
        module = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        script = CustomScript.objects.create(project=self.project, module_path='deploy', class_name='Deploy')

        for instance in (self.project, revision, module, script):
            with self.subTest(model=type(instance).__name__):
                self.assertEqual(serialize_for_event(instance)['id'], instance.pk)


class EventDispatchTestCase(TestCase):
    """That a rule matching a plugin object reaches the queue."""

    def setUp(self):
        super().setUp()
        # The queue is isolated by testing/configuration.py, so this only separates one test from
        # the next. Never clear with NetBox's RQQueueTestMixin, which uses a server-wide flushall().
        self.queue = django_rq.get_queue('default')
        self.queue.empty()
        self.addCleanup(self.queue.empty)
        self.project = ScriptProject.objects.create(name='Dispatched', key='dispatched')
        self.object_type = ObjectType.objects.get_for_model(ScriptProject)
        webhook = Webhook.objects.create(name='Receiver', payload_url='http://localhost/hook/')
        self.rule = EventRule.objects.create(
            name='On a new project',
            event_types=[OBJECT_CREATED],
            action_type=EventRuleActionChoices.WEBHOOK,
            action_object=webhook,
        )
        self.rule.object_types.set([self.object_type])

    def event(self):
        """Build the event a created project produces, in the shape the pipeline is handed."""
        return {
            'event_type': OBJECT_CREATED,
            'data': serialize_for_event(self.project),
            'snapshots': {'prechange': None, 'postchange': {'key': self.project.key}},
        }

    def dispatch(self):
        """Run the pipeline so that the enqueue actually happens."""
        # django_rq defers an enqueue to transaction.on_commit under its default on_db_commit
        # mode, and a TestCase never commits, so without this the queue stays empty.
        with self.captureOnCommitCallbacks(execute=True):
            process_event_rules([self.rule], self.object_type, self.event())

    def test_a_matching_rule_queues_one_task(self):
        self.dispatch()

        self.assertEqual(self.queue.count, 1)
        queued = self.queue.jobs[0]
        self.assertEqual(queued.kwargs['event_rule'], self.rule)
        self.assertEqual(queued.kwargs['object_type'], self.object_type)
        self.assertEqual(queued.kwargs['data']['key'], 'dispatched')

    def test_a_rule_whose_condition_fails_queues_nothing(self):
        self.rule.conditions = {'attr': 'key', 'value': 'something-else'}
        self.rule.save()

        self.dispatch()

        self.assertEqual(self.queue.count, 0)

    def test_a_rule_whose_condition_matches_queues_the_task(self):
        self.rule.conditions = {'attr': 'key', 'value': 'dispatched'}
        self.rule.save()

        self.dispatch()

        self.assertEqual(self.queue.count, 1)

    def test_an_event_without_a_request_dispatches_without_one(self):
        # A Job lifecycle event carries no request, and the pipeline must not assume one.
        self.dispatch()

        self.assertEqual(self.queue.count, 1)
        self.assertNotIn('request', self.queue.jobs[0].kwargs)
