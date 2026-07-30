import sys
import tempfile
import uuid
from pathlib import Path

from django.test import override_settings
from django.urls import reverse

from core.models import Job, ObjectType
from extras.models import Tag
from netbox_custom_scripts.activation import activate_revision, deactivate_revision
from netbox_custom_scripts.models import CustomScript, CustomScriptModule, CustomScriptProject
from netbox_custom_scripts.runtime.naming import PRIVATE_ROOT
from netbox_custom_scripts.storage import service
from netbox_custom_scripts.tests.runtime.test_cache import discard_tree
from netbox_custom_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_custom_scripts.validation import validate_revision
from users.models import ObjectPermission
from utilities.testing import TestCase

TAKES_A_NAME = (
    b'from netbox_custom_scripts.scripts import Script, StringVar\n'
    b'from extras.models import Tag\n\n\n'
    b'class MakeTag(Script):\n'
    b'    class Meta:\n'
    b"        name = 'Make One Tag'\n\n"
    b"    label = StringVar(label='Label')\n\n"
    b'    def run(self, data, commit):\n'
    b"        Tag.objects.create(name=data['label'], slug='made-by-run')\n"
    b"        self.log_success('Created ' + data['label'])\n"
    b"        return data['label']\n"
)


class RunViewTestMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.cache_root = Path(tempfile.mkdtemp())
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        self.project = CustomScriptProject.objects.create(name='Runnable', key='runnable')
        self.revision = self.publish()
        self.script = CustomScript.objects.get(project=self.project)

    def _purge_namespace(self):
        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def publish(self, source=TAKES_A_NAME):
        """Stage, validate and activate one tree, so the project is serving a runnable script."""
        CustomScriptModule.objects.get_or_create(
            project=self.project, source_path='deploy.py', defaults={'enabled': True}
        )
        revision, _ = service.stage_revision(self.project, {'deploy.py': source})
        revision = validate_revision(revision, job=Job.objects.create(name='validation', job_id=uuid.uuid4()))
        activate_revision(revision)
        self.project.refresh_from_db()
        return revision

    def grant(self, *actions, model=CustomScript):
        """Grant the named actions on one model to the test user."""
        permission = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))

    def url(self, name='run', **kwargs):
        return reverse(f'plugins:netbox_custom_scripts:customscript_{name}', kwargs={'pk': self.script.pk, **kwargs})


class RunViewTestCase(RunViewTestMixin, TestCase):
    def test_the_run_page_renders_the_form_the_source_declares(self):
        self.grant('view', 'run')

        response = self.client.get(self.url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('name="label"', content)
        self.assertIn('name="_commit"', content)

    def test_the_run_page_is_refused_without_the_run_action(self):
        # View alone is not enough, running is its own permission.
        self.grant('view', 'change')

        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_a_run_is_refused_without_the_run_action(self):
        self.grant('view', 'change')

        self.assertHttpStatus(self.client.post(self.url(), {'label': 'Anything', '_commit': 'on'}), 403)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_the_run_page_explains_itself_when_the_script_cannot_run(self):
        self.grant('view', 'run')
        deactivate_revision(self.revision)

        response = self.client.get(self.url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('cannot be run', content)
        self.assertNotIn('name="label"', content)

    def test_a_valid_submission_queues_one_run_and_redirects_to_it(self):
        self.grant('view', 'run')

        response = self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on'})

        job = Job.objects.get(object_id=self.script.pk)
        self.assertRedirects(
            response,
            self.url('result', job_pk=job.pk),
            fetch_redirect_response=False,
        )
        self.assertEqual(job.data['module_path'], 'deploy')
        self.assertEqual(job.data['class_name'], 'MakeTag')
        self.assertEqual(job.data['revision_digest'], self.revision.digest)

    def test_an_unchecked_commit_box_queues_a_dry_run(self):
        self.grant('view', 'run')

        self.client.post(self.url(), {'label': 'Queued Tag'})

        self.assertIs(Job.objects.get(object_id=self.script.pk).data['commit'], False)

    def test_an_invalid_submission_re_renders_the_form_and_queues_nothing(self):
        self.grant('view', 'run')

        response = self.client.post(self.url(), {'_commit': 'on'})

        self.assertHttpStatus(response, 200)
        self.assertIn('name="label"', response.content.decode())
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_the_queued_run_records_the_requesting_user(self):
        self.grant('view', 'run')

        self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on'})

        self.assertEqual(Job.objects.get(object_id=self.script.pk).user, self.user)


class ResultViewTestCase(RunViewTestMixin, TestCase):
    def finished_job(self, **overrides):
        job = Job.objects.create(
            name='Run Custom Script',
            job_id=uuid.uuid4(),
            object_type=ObjectType.objects.get_for_model(CustomScript),
            object_id=self.script.pk,
            user=self.user,
        )
        job.data = {
            'revision_digest': self.revision.digest,
            'module_path': 'deploy',
            'class_name': 'MakeTag',
            'output': 'Queued Tag',
            'log': [
                {'time': '2026-07-30T12:00:00+00:00', 'status': 'debug', 'message': 'a debug line', 'obj': None},
                {'time': '2026-07-30T12:00:01+00:00', 'status': 'success', 'message': 'a success line', 'obj': None},
            ],
            **overrides,
        }
        job.save()
        return job

    def test_the_result_page_renders_the_log_and_the_output(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        response = self.client.get(self.url('result', job_pk=job.pk))

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('a success line', content)
        self.assertIn('Queued Tag', content)

    def test_records_below_the_threshold_are_left_out(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        default = self.client.get(self.url('result', job_pk=job.pk)).content.decode()
        verbose = self.client.get(f'{self.url("result", job_pk=job.pk)}?log_threshold=debug').content.decode()

        self.assertNotIn('a debug line', default)
        self.assertIn('a debug line', verbose)

    def test_a_job_belonging_to_another_script_is_not_found(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        other = Job.objects.create(name='Elsewhere', job_id=uuid.uuid4())

        self.assertHttpStatus(self.client.get(self.url('result', job_pk=other.pk)), 404)


class RunEndToEndTestCase(RunViewTestMixin, TestCase):
    def test_a_submitted_run_executes_and_records_what_it_did(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)

        self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on'})
        job = Job.objects.get(object_id=self.script.pk)
        # The queue is not running under test, so the worker's side is driven directly with the
        # payload the view recorded.
        from netbox_custom_scripts.jobs import CustomScriptJob

        CustomScriptJob.handle(job, **job.data, data={'label': 'Queued Tag'}, request=None)
        job.refresh_from_db()

        self.assertTrue(Tag.objects.filter(slug='made-by-run').exists())
        self.assertEqual(job.data['output'], 'Queued Tag')
        self.assertIn('Created Queued Tag', [entry['message'] for entry in job.data['log']])


class RunButtonTestCase(RunViewTestMixin, TestCase):
    """
    The Run button on the list, which the static route guard cannot see.

    `test_actions.py` iterates `ActionsColumn.actions`, and Run is an extra button, so only a
    real render proves it resolves and that it is gated.
    """

    def list_url(self):
        return reverse('plugins:netbox_custom_scripts:customscript_list')

    def test_the_list_offers_a_run_button_to_a_permitted_user(self):
        self.grant('view', 'run')

        response = self.client.get(self.list_url())

        self.assertHttpStatus(response, 200)
        self.assertIn(self.url('run'), response.content.decode())

    def test_the_list_offers_no_run_button_without_the_run_action(self):
        self.grant('view', 'change')

        response = self.client.get(self.list_url())

        self.assertHttpStatus(response, 200)
        self.assertNotIn(self.url('run'), response.content.decode())

    def test_the_button_is_inert_for_a_script_that_cannot_run(self):
        self.grant('view', 'run')
        deactivate_revision(self.revision)

        content = self.client.get(self.list_url()).content.decode()

        self.assertNotIn(self.url('run'), content)
        # Specific to our own button, so a stray "disabled" elsewhere on the page cannot pass it.
        self.assertIn('This Custom Script cannot be run right now.', content)

    def test_the_detail_page_offers_the_run_button_too(self):
        self.grant('view', 'run')

        response = self.client.get(self.script.get_absolute_url())

        self.assertHttpStatus(response, 200)
        self.assertIn(self.url('run'), response.content.decode())


class ExecutionDefaultsPanelTestCase(RunViewTestMixin, TestCase):
    """The detail page used to print the metadata dict verbatim."""

    def test_the_defaults_render_as_labelled_rows_not_json(self):
        self.grant('view', 'run')
        CustomScript.objects.filter(pk=self.script.pk).update(
            metadata={
                'commit_default': True,
                'scheduling_enabled': True,
                'job_timeout': 600,
                'notifications_default': 'on_failure',
            }
        )

        content = self.client.get(self.script.get_absolute_url()).content.decode()

        self.assertIn('Run timeout', content)
        self.assertIn('600 seconds', content)
        self.assertIn('Notifications', content)
        self.assertIn('On failure', content)
        # The raw dict shape is what the complaint was about.
        self.assertNotIn('&#x27;notifications_default&#x27;', content)
        self.assertNotIn('commit_default', content)

    def test_no_timeout_reads_as_the_system_default(self):
        self.grant('view', 'run')

        content = self.client.get(self.script.get_absolute_url()).content.decode()

        self.assertIn('System default', content)
