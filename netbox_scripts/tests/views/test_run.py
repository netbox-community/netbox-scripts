import sys
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import override_settings
from django.urls import reverse

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from extras.models import Tag
from netbox_scripts.activation import activate_revision, deactivate_revision
from netbox_scripts.jobs import CustomScriptJob
from netbox_scripts.models import CustomScript, ScriptFile, ScriptProject
from netbox_scripts.runtime.exceptions import EntrypointImportError, LocalCacheError
from netbox_scripts.runtime.naming import PRIVATE_ROOT
from netbox_scripts.scripts.logging import LogLevelChoices
from netbox_scripts.storage import service
from netbox_scripts.tests.runtime.test_cache import discard_tree
from netbox_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_scripts.validation import validate_revision
from users.models import ObjectPermission
from utilities.datetime import local_now
from utilities.testing import TestCase

TAKES_A_NAME = (
    b'from netbox_scripts.scripts import Script, StringVar\n'
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
            override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        self.project = ScriptProject.objects.create(name='Runnable', key='runnable')
        self.revision = self.publish()
        self.script = CustomScript.objects.get(project=self.project)

    def _purge_namespace(self):
        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def publish(self, source=TAKES_A_NAME):
        """Stage, validate and activate one tree, so the project is serving a runnable script."""
        ScriptFile.objects.get_or_create(project=self.project, source_path='deploy.py', defaults={'enabled': True})
        revision, _ = service.stage_revision(self.project, {'deploy.py': source})
        revision = validate_revision(revision, job=Job.objects.create(name='validation', job_id=uuid.uuid4()))
        activate_revision(revision)
        self.project.refresh_from_db()
        return revision

    def grant(self, *actions, model=CustomScript, constraints=None):
        """Grant the named actions on one model to the test user, optionally constrained."""
        permission = ObjectPermission(
            name=f'{model._meta.model_name} {"/".join(actions)}',
            actions=list(actions),
            constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))

    def url(self, name='run', **kwargs):
        return reverse(f'plugins:netbox_scripts:customscript_{name}', kwargs={'pk': self.script.pk, **kwargs})


class RunViewTestCase(RunViewTestMixin, TestCase):
    def test_the_run_page_renders_the_form_the_source_declares(self):
        self.grant('view', 'run')

        response = self.client.get(self.url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('name="label"', content)
        self.assertIn('name="_commit"', content)

    def test_no_template_syntax_reaches_the_browser(self):
        # A {# #} comment is single-line only in Django, so a two-line one renders as text.
        self.grant('view', 'run')

        content = self.client.get(self.url()).content.decode()

        for token in ('{#', '#}', '{%', '%}'):
            self.assertNotIn(token, content, f'raw template syntax {token} leaked into the run page')

    def test_the_run_page_carries_the_standard_tab_strip(self):
        # Every NetBox page has one, and it is the only way back to the object from here.
        self.grant('view', 'run')

        content = self.client.get(self.url()).content.decode()

        self.assertIn('nav nav-tabs', content)
        self.assertIn(f'href="{self.script.get_absolute_url()}"', content)
        self.assertIn(f'href="{self.url()}" class="nav-link active"', content)

    def test_the_run_tab_appears_on_the_other_views(self):
        # Registered as a ViewTab, so every view of the script offers it, not just this page.
        self.grant('view', 'run')

        for page in ('', 'changelog/', 'jobs/'):
            with self.subTest(page=page or 'detail'):
                content = self.client.get(f'{self.script.get_absolute_url()}{page}').content.decode()
                self.assertIn(f'href="{self.url()}"', content)

    def test_the_run_tab_is_hidden_without_the_run_permission(self):
        self.grant('view', 'change')

        content = self.client.get(self.script.get_absolute_url()).content.decode()

        self.assertNotIn(f'href="{self.url()}"', content)

    def test_the_run_page_names_the_script_it_will_run(self):
        # The dotted name is the identity an author works with.
        self.grant('view', 'run')

        content = self.client.get(self.url()).content.decode()

        identifier = ' '.join(content[content.find('<code class="d-block text-muted') :][:300].split())
        self.assertIn('deploy.MakeTag', identifier)
        self.assertNotIn('netbox_scripts.customscript', identifier)

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
        # Deactivation retires the script, so retirement is the first condition that holds.
        self.assertIn('It is retired', content)
        self.assertNotIn('name="label"', content)

    def test_the_run_page_names_a_project_serving_no_revision_rather_than_guessing(self):
        self.grant('view', 'run')
        ScriptProject.objects.filter(pk=self.script.project_id).update(active_revision=None)

        content = self.client.get(self.url()).content.decode()

        self.assertIn('Its Project is serving no revision.', content)
        self.assertNotIn('disabled or retired', content)

    def test_a_load_failure_outside_the_narrow_set_re_renders_with_the_reason(self):
        # EntrypointImportError is not rooted in StorageError, so the narrow tuple missed it.
        self.grant('view', 'run')

        with patch(
            'netbox_scripts.views.scripts.load_script_class',
            side_effect=EntrypointImportError('deploy imports a module that is not there', {}),
        ):
            response = self.client.get(self.url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertIn('could not be loaded', content)
        self.assertNotIn('name="label"', content)

    def test_a_cache_failure_leaks_no_storage_identity_into_the_page(self):
        self.grant('view', 'run')
        key = str(self.project.storage_key)
        leaky = LocalCacheError(f'/var/cache/{key}/{self.revision.digest}/deploy.py could not be read')

        with patch('netbox_scripts.execution.resolve_script_class', side_effect=leaky):
            response = self.client.get(self.url())

        self.assertHttpStatus(response, 200)
        content = response.content.decode()
        self.assertNotIn(key, content)
        self.assertNotIn(self.revision.digest, content)

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

    def test_the_scheduling_fields_are_absent_without_the_schedule_permission(self):
        # Withheld by omission, so an operator is never offered a choice that is then refused.
        self.grant('view', 'run')

        content = self.client.get(self.url()).content.decode()

        self.assertNotIn('name="_schedule_at"', content)
        self.assertNotIn('name="_interval"', content)
        # The rest of the group survives, which a fieldset naming an absent field drops.
        self.assertIn('name="_notifications"', content)
        self.assertIn('name="_commit"', content)

    def test_the_scheduling_fields_are_absent_when_the_grant_excludes_this_script(self):
        # has_perm returns True on a bare codename, so the object is what applies the constraint.
        self.grant('view', 'run')
        self.grant('schedule', constraints={'project__key': 'somewhere-else'})

        response = self.client.get(self.url())

        content = response.content.decode()
        self.assertNotIn('name="_schedule_at"', content)
        self.assertNotIn('name="_interval"', content)

    def test_the_scheduling_fields_appear_with_the_schedule_permission(self):
        self.grant('view', 'run', 'schedule')

        content = self.client.get(self.url()).content.decode()

        self.assertIn('name="_schedule_at"', content)
        self.assertIn('name="_interval"', content)

    def test_a_schedule_submitted_without_the_permission_is_ignored(self):
        # A form that never carried the field cannot receive one, so the run goes ahead now.
        self.grant('view', 'run')
        when = local_now() + timedelta(hours=1)

        self.client.post(
            self.url(),
            {'label': 'Queued Tag', '_commit': 'on', '_schedule_at': when.strftime('%Y-%m-%d %H:%M:%S')},
        )

        job = Job.objects.get(object_id=self.script.pk)
        self.assertIsNone(job.scheduled)

    def test_a_submitted_schedule_defers_the_run(self):
        self.grant('view', 'run', 'schedule')
        when = local_now() + timedelta(hours=1)

        self.client.post(
            self.url(),
            {'label': 'Queued Tag', '_commit': 'on', '_schedule_at': when.strftime('%Y-%m-%d %H:%M:%S')},
        )

        job = Job.objects.get(object_id=self.script.pk)
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)
        self.assertIsNotNone(job.scheduled)

    def test_a_submitted_interval_makes_the_run_recurring(self):
        self.grant('view', 'run', 'schedule')

        self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on', '_interval': '60'})

        job = Job.objects.get(object_id=self.script.pk)
        self.assertEqual(job.interval, 60)
        # A recurrence resolves per occurrence, so it carries no pin.
        self.assertIsNone(job.data['revision_id'])

    def test_the_execution_parameters_never_reach_the_script_as_variables(self):
        # The Job row deliberately records no input values, so what the view forwarded has to
        # be observed at the call rather than read back off the row.
        self.grant('view', 'run', 'schedule')
        captured = {}
        original = CustomScriptJob.enqueue_run

        def record(script, **kwargs):
            captured.update(kwargs)
            return original(script, **kwargs)

        with patch.object(CustomScriptJob, 'enqueue_run', record):
            self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on', '_interval': '60'})

        self.assertEqual(set(captured['data']), {'label'})
        self.assertEqual(captured['interval'], 60)
        self.assertIs(captured['commit'], True)

    def test_a_past_schedule_re_renders_the_form_and_queues_nothing(self):
        self.grant('view', 'run', 'schedule')
        when = local_now() - timedelta(hours=1)

        response = self.client.post(
            self.url(),
            {'label': 'Queued Tag', '_commit': 'on', '_schedule_at': when.strftime('%Y-%m-%d %H:%M:%S')},
        )

        self.assertHttpStatus(response, 200)
        self.assertIn('must be in the future', response.content.decode())
        self.assertFalse(Job.objects.filter(object_id=self.script.pk).exists())


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

    def test_a_job_of_another_type_sharing_the_key_is_not_found(self):
        # Repointed history is the first thing to put foreign Jobs onto plugin object ids.
        self.grant('view', 'run')
        self.grant('view', model=Job)
        foreign = Job.objects.create(
            name='something else',
            job_id=uuid.uuid4(),
            object_type=ObjectType.objects.get_for_model(ScriptProject),
            object_id=self.script.pk,
            user=self.user,
        )

        response = self.client.get(self.url('result', job_pk=foreign.pk))

        self.assertHttpStatus(response, 404)

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

    def test_no_template_syntax_reaches_the_browser(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        for token in ('{#', '#}', '{%', '%}'):
            self.assertNotIn(token, content, f'raw template syntax {token} leaked into the result page')

    def test_the_result_page_carries_the_standard_tab_strip(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertIn('nav nav-tabs', content)
        self.assertIn(f'href="{self.script.get_absolute_url()}"', content)
        self.assertIn('nav-link active', content)

    def test_a_job_belonging_to_another_script_is_not_found(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        other = Job.objects.create(name='Elsewhere', job_id=uuid.uuid4())

        self.assertHttpStatus(self.client.get(self.url('result', job_pk=other.pk)), 404)

    def test_every_threshold_is_offered_and_the_active_one_marked(self):
        # Implemented in the view, so without a control it is reachable only by URL.
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        content = self.client.get(f'{self.url("result", job_pk=job.pk)}?log_threshold=warning').content.decode()

        for level in LogLevelChoices.values():
            self.assertIn(f'?log_threshold={level}', content)
        self.assertIn('Warning', content)

    def test_an_unknown_threshold_falls_back_to_info(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        response = self.client.get(f'{self.url("result", job_pk=job.pk)}?log_threshold=nonsense')

        self.assertEqual(response.context['log_threshold'], LogLevelChoices.LOG_INFO)

    def test_a_finished_run_does_not_poll(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_COMPLETED
        job.save()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertNotIn('hx-trigger', content)

    def test_a_run_in_flight_polls_itself(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_RUNNING
        job.save()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertIn('hx-trigger="every 5s"', content)
        self.assertIn('hx-swap="outerHTML"', content)

    def test_a_scheduled_run_polls_less_often(self):
        # A scheduled run can be days out, so it is not worth the rate of one already moving.
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_SCHEDULED
        job.save()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertIn('hx-trigger="every 60s"', content)

    def test_the_poll_keeps_the_chosen_threshold(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_RUNNING
        job.save()

        content = self.client.get(f'{self.url("result", job_pk=job.pk)}?log_threshold=debug').content.decode()

        self.assertIn('log_threshold=debug" hx-trigger', content)

    def test_a_poll_returns_the_body_alone(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_RUNNING
        job.save()

        content = self.client.get(self.url('result', job_pk=job.pk), headers={'hx-request': 'true'}).content.decode()

        self.assertIn('id="run-result"', content)
        # No page furniture, which is the point of serving the partial
        self.assertNotIn('breadcrumb', content)
        self.assertNotIn('Log threshold', content)

    def test_a_scheduled_run_says_when_and_how_often(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()
        job.status = JobStatusChoices.STATUS_SCHEDULED
        job.scheduled = local_now() + timedelta(hours=3)
        job.interval = 60
        job.save()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertIn('Scheduled for', content)
        self.assertIn('Recurs every', content)
        self.assertIn('60 minutes', content)

    def test_an_immediate_run_says_neither(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertNotIn('Scheduled for', content)
        self.assertNotIn('Recurs every', content)

    def test_the_table_can_be_configured(self):
        # table.configure() reads a saved configuration, and the modal is what writes one.
        self.grant('view', 'run')
        self.grant('view', model=Job)
        job = self.finished_job()

        content = self.client.get(self.url('result', job_pk=job.pk)).content.decode()

        self.assertIn('id="CustomScriptLogTable_config"', content)
        self.assertIn('data-bs-target="#CustomScriptLogTable_config"', content)


class RunEndToEndTestCase(RunViewTestMixin, TestCase):
    def test_a_submitted_run_executes_and_records_what_it_did(self):
        self.grant('view', 'run')
        self.grant('view', model=Job)

        self.client.post(self.url(), {'label': 'Queued Tag', '_commit': 'on'})
        job = Job.objects.get(object_id=self.script.pk)
        # The queue is not running under test, so the worker's side is driven directly with the
        # payload the view recorded.
        from netbox_scripts.jobs import CustomScriptJob

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
        return reverse('plugins:netbox_scripts:customscript_list')

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
        self.assertIn('It is retired', content)

    def test_the_detail_page_offers_the_run_button_too(self):
        self.grant('view', 'run')

        response = self.client.get(self.script.get_absolute_url())

        self.assertHttpStatus(response, 200)
        self.assertIn(self.url('run'), response.content.decode())

    def test_the_detail_page_button_is_inert_and_names_the_reason(self):
        # The only render of buttons/run.html's disabled branch. An unresolvable context name
        # would leave the tooltip silently truncated rather than failing.
        self.grant('view', 'run')
        deactivate_revision(self.revision)

        content = self.client.get(self.script.get_absolute_url()).content.decode()

        # The Run ViewTab is gated on the permission rather than on executability, so its link
        # is still here. It is the button that goes inert.
        self.assertIn('disabled', content)
        self.assertIn('This Custom Script cannot be run right now. It is retired', content)


class OverriddenDefaultsTestCase(RunViewTestMixin, TestCase):
    """An operator's override has to reach the run form, not stop at the row."""

    def setUp(self):
        super().setUp()
        self.grant('view', 'run')

    def rendered_form(self):
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200)
        return response.context['form']

    def test_the_commit_toggle_follows_the_override_rather_than_the_class(self):
        # The class declares commit_default True by omission, so False can only come from
        # the operator's column.
        CustomScript.objects.filter(pk=self.script.pk).update(commit_default_override=False)

        self.assertIs(self.rendered_form().fields['_commit'].initial, False)

    def test_the_notification_field_follows_the_override_rather_than_the_class(self):
        CustomScript.objects.filter(pk=self.script.pk).update(notifications_default_override='never')

        self.assertEqual(self.rendered_form().fields['_notifications'].initial, 'never')

    def test_a_run_submitted_from_the_rendered_page_carries_the_override(self):
        # The rendered initial is the whole contract for the toggle: an unchecked box submits
        # False whatever the default was, so what matters is what the operator is shown.
        CustomScript.objects.filter(pk=self.script.pk).update(commit_default_override=False)
        form = self.rendered_form()

        self.assertIs(form.fields['_commit'].initial, False)
        self.assertNotIn('checked', str(form['_commit']))


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
