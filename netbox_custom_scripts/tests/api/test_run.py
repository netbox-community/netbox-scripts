import uuid
from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from rest_framework import status

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from netbox_custom_scripts.activation import activate_revision
from netbox_custom_scripts.jobs import CustomScriptJob
from netbox_custom_scripts.models import CustomScript, CustomScriptModule, CustomScriptProject
from netbox_custom_scripts.storage import service
from netbox_custom_scripts.tests.plugin_testing import PluginAPIViewTestCase
from netbox_custom_scripts.tests.views.test_run import TAKES_A_NAME, RunViewTestMixin
from netbox_custom_scripts.validation import validate_revision
from users.models import ObjectPermission
from utilities.datetime import local_now
from utilities.testing import APITestCase

# Declared unsafe to run unattended, so this endpoint must refuse a schedule for it.
UNSCHEDULABLE = (
    b'from netbox_custom_scripts.scripts import Script\n\n\n'
    b'class RunNow(Script):\n'
    b'    class Meta:\n'
    b"        name = 'Run It Now'\n"
    b'        scheduling_enabled = False\n\n'
    b'    def run(self, data, commit):\n'
    b"        self.log_success('ran')\n"
)

# The same script under a different comment, so it stages to a different digest.
TAKES_A_NAME_AGAIN = b'# a second revision of the same script\n' + TAKES_A_NAME


class RunAPITestCase(RunViewTestMixin, PluginAPIViewTestCase, APITestCase):
    """The run action: what it enqueues, what it validates, and what it refuses."""

    model = CustomScript

    def setUp(self):
        super().setUp()
        # No queue worker runs under test, and the endpoint refuses a run nothing can pick up.
        self.enterContext(patch('netbox_custom_scripts.api.views.any_workers_for_queue', return_value=True))

    def run_url(self, script=None):
        """Return the run route for one script, defaulting to the fixture's own."""
        viewname = f'{self._get_view_namespace()}:customscript-run'
        return reverse(viewname, kwargs={'pk': (script or self.script).pk})

    def post_run(self, body=None, script=None):
        """POST one run request, with the envelope body as given."""
        return self.client.post(self.run_url(script), body or {}, format='json', **self.header)

    def publish_elsewhere(self, source):
        """Serve one script from a second project, so the fixture's own script is undisturbed."""
        project = CustomScriptProject.objects.create(name='Other', key='other')
        CustomScriptModule.objects.create(project=project, source_path='now.py', enabled=True)
        revision, _ = service.stage_revision(project, {'now.py': source})
        revision = validate_revision(revision, job=Job.objects.create(name='validation', job_id=uuid.uuid4()))
        activate_revision(revision)
        return CustomScript.objects.get(project=project)

    def test_an_omitted_commit_falls_back_to_the_scripts_effective_default(self):
        # The fallback has to read the row, so an operator's override applies to a caller that
        # does not state an intent. The class declares commit_default True by omission.
        self.grant('view', 'run')
        CustomScript.objects.filter(pk=self.script.pk).update(commit_default_override=False)

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertIs(Job.objects.get(pk=response.data['id']).data['commit'], False)

    def test_a_run_is_enqueued_and_the_job_is_returned(self):
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'commit': True})

        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        self.assertEqual(Job.objects.get(pk=response.data['id']).object_id, self.script.pk)

    def test_the_response_is_the_job_rather_than_the_script(self):
        # A caller polls the Job identity rather than guessing which of the script's jobs it was.
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertTrue(response.data['url'].endswith(f'/api/core/jobs/{response.data["id"]}/'))
        self.assertEqual(response.data['object_id'], self.script.pk)
        self.assertNotIn('class_name', response.data)

    def test_the_envelope_carries_the_variable_values_to_the_run(self):
        # The Job row records no input values, so this is observed at the call.
        self.grant('view', 'run')
        captured = {}
        original = CustomScriptJob.enqueue_run

        def record(script, **kwargs):
            captured.update(kwargs)
            return original(script, **kwargs)

        with patch.object(CustomScriptJob, 'enqueue_run', record):
            self.post_run({'data': {'label': 'made-over-rest'}, 'commit': False})

        self.assertEqual(captured['data'], {'label': 'made-over-rest'})
        self.assertIs(captured['commit'], False)

    def test_an_execution_parameter_never_reaches_the_script_as_a_variable(self):
        self.grant('view', 'run', 'schedule')
        captured = {}
        original = CustomScriptJob.enqueue_run

        def record(script, **kwargs):
            captured.update(kwargs)
            return original(script, **kwargs)

        with patch.object(CustomScriptJob, 'enqueue_run', record):
            self.post_run({'data': {'label': 'made-over-rest'}, 'interval': 60})

        self.assertEqual(set(captured['data']), {'label'})
        self.assertEqual(captured['interval'], 60)

    def test_a_flat_body_is_not_read_as_variable_values(self):
        # Values sent beside the execution parameters are not variables at all.
        self.grant('view', 'run')

        response = self.post_run({'label': 'made-over-rest', 'commit': True})

        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn('label', response.data)

    def test_an_omitted_commit_takes_the_default_the_script_class_declares(self):
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertIs(Job.objects.get(pk=response.data['id']).data['commit'], True)

    def test_an_invalid_variable_value_is_refused_by_the_class_form(self):
        # The declared variables are the only authority on what is valid.
        self.grant('view', 'run')

        response = self.post_run({'data': {}})

        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn('label', response.data)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_the_list_route_still_refuses_post(self):
        # Routing a detail action must not reopen creation on a derived model.
        self.grant('view', 'run', 'add')

        response = self.client.post(self._get_list_url(), {}, format='json', **self.header)

        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_a_disabled_script_is_refused_rather_than_queued(self):
        self.grant('view', 'run')
        self.script.enabled = False
        self.script.save()

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_change_permission_alone_cannot_run(self):
        # Running a script and editing its administrative fields are different privileges.
        self.grant('view', 'change')

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_the_add_permission_is_not_what_this_route_asks_for(self):
        # Token permissions resolve a POST to add, and the base viewset narrows a POST's queryset
        # by add. Rows are derived, so either default would refuse every caller.
        self.grant('view', 'add')

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)

    def test_an_object_constraint_narrows_what_can_be_run(self):
        # Granted on the model, constrained to another project, so the override must still bite.
        permission = ObjectPermission(name='run elsewhere', actions=['run'], constraints={'project__key': 'other'})
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(CustomScript))

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_a_one_shot_run_pins_the_revision_it_was_requested_against(self):
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}})
        activate_revision(self.publish(source=TAKES_A_NAME_AGAIN))

        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual(job.data['revision_digest'], self.revision.digest)

    def test_a_scheduled_run_is_accepted_and_the_job_is_scheduled(self):
        self.grant('view', 'run', 'schedule')
        when = local_now() + timedelta(hours=1)

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'schedule_at': when.isoformat()})

        self.assertHttpStatus(response, status.HTTP_201_CREATED)
        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(job.scheduled, when)

    def test_a_recurring_run_records_its_interval_and_pins_nothing(self):
        # A pinned recurrence would execute one frozen revision forever.
        self.grant('view', 'run', 'schedule')

        response = self.post_run(
            {
                'data': {'label': 'made-over-rest'},
                'schedule_at': (local_now() + timedelta(minutes=5)).isoformat(),
                'interval': 60,
            }
        )

        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual(job.interval, 60)
        self.assertIsNone(job.data['revision_id'])

    def test_a_past_schedule_is_refused(self):
        self.grant('view', 'run', 'schedule')
        when = local_now() - timedelta(hours=1)

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'schedule_at': when.isoformat()})

        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn('schedule_at', response.data)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_an_interval_with_no_start_schedules_from_now(self):
        # A recurrence alone means "from now", the only reading that does not discard it.
        self.grant('view', 'run', 'schedule')

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'interval': 60})

        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual(job.interval, 60)
        self.assertIsNotNone(job.scheduled)

    def test_scheduling_is_refused_when_the_script_class_forbids_it(self):
        # The run form omits the fields, which is presentation. REST has to refuse the value.
        self.grant('view', 'run', 'schedule')
        script = self.publish_elsewhere(UNSCHEDULABLE)

        response = self.post_run({'data': {}, 'interval': 60}, script=script)

        self.assertHttpStatus(response, status.HTTP_400_BAD_REQUEST)
        self.assertIn('interval', response.data)
        self.assertFalse(Job.objects.filter(object_id=script.pk).exists())

    def test_an_unschedulable_script_still_runs_immediately(self):
        self.grant('view', 'run')
        script = self.publish_elsewhere(UNSCHEDULABLE)

        response = self.post_run({'data': {}}, script=script)

        self.assertHttpStatus(response, status.HTTP_201_CREATED)

    def test_a_scheduled_run_is_refused_without_the_schedule_permission(self):
        # The run form withholds the fields, which is presentation. Here the value is refused.
        self.grant('view', 'run')
        when = local_now() + timedelta(hours=1)

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'schedule_at': when.isoformat()})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_a_recurring_run_is_refused_without_the_schedule_permission(self):
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}, 'interval': 60})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_run_without_schedule_still_runs_immediately(self):
        # The separation only works if withholding the schedule leaves running intact.
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_201_CREATED)

    def test_no_running_worker_is_refused_with_service_unavailable(self):
        # A run nothing can pick up would sit in the queue with no signal.
        self.grant('view', 'run')

        with patch('netbox_custom_scripts.api.views.any_workers_for_queue', return_value=False):
            response = self.post_run({'data': {'label': 'made-over-rest'}})

        self.assertHttpStatus(response, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())

    def test_a_requested_run_executes_and_records_what_it_did(self):
        self.grant('view', 'run')

        response = self.post_run({'data': {'label': 'made-over-rest'}})
        job = Job.objects.get(pk=response.data['id'])
        # The queue is not running under test, so the worker's side is driven directly.
        CustomScriptJob.handle(job, **job.data, data={'label': 'made-over-rest'}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.data['output'], 'made-over-rest')
        self.assertIn('Created made-over-rest', [entry['message'] for entry in job.data['log']])
