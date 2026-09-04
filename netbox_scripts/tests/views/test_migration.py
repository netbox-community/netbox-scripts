import shutil
import tempfile
import uuid
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from extras.models import ScriptModule
from netbox_scripts.choices import (
    MigrationStateChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_scripts.jobs import (
    MigrationActivationJob,
    MigrationCleanupJob,
    MigrationCutoverJob,
    MigrationInventoryJob,
    MigrationReferencesJob,
    MigrationStagingJob,
    MigrationVerificationJob,
)
from netbox_scripts.migration import cutover, mapping
from netbox_scripts.models import CustomScriptProject, MigrationRun, ScriptProjectRevision
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user


class MigrationTriggerTestCase(TestCase):
    """The Migration page and the two routes that queue a pass."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        # Job.enqueue() reaches the shared Redis, which a test run must not touch.
        self.inventory = self.enterContext(
            mock.patch.object(MigrationInventoryJob, 'enqueue', side_effect=self.fake_job)
        )
        self.staging = self.enterContext(mock.patch.object(MigrationStagingJob, 'enqueue', side_effect=self.fake_job))
        self.cutover = self.enterContext(mock.patch.object(MigrationCutoverJob, 'enqueue', side_effect=self.fake_job))
        self.activation = self.enterContext(
            mock.patch.object(MigrationActivationJob, 'enqueue', side_effect=self.fake_job)
        )
        self.references = self.enterContext(
            mock.patch.object(MigrationReferencesJob, 'enqueue', side_effect=self.fake_job)
        )
        self.cleanup = self.enterContext(mock.patch.object(MigrationCleanupJob, 'enqueue', side_effect=self.fake_job))
        self.verification = self.enterContext(
            mock.patch.object(MigrationVerificationJob, 'enqueue', side_effect=self.fake_job)
        )

    @staticmethod
    def fake_job(**kwargs):
        """Stand in for an enqueue, returning a Job row the view can redirect to."""
        return Job.objects.create(name='queued', job_id=uuid.uuid4(), user=kwargs.get('user'))

    def grant(self, *actions):
        obj_perm = ObjectPermission(name=f'project {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CustomScriptProject))

    @staticmethod
    def url(name):
        return reverse(f'plugins:netbox_scripts:{name}')

    def record(self, job_class, status=JobStatusChoices.STATUS_COMPLETED):
        return Job.objects.create(name=job_class.name, job_id=uuid.uuid4(), status=status)

    def test_the_page_needs_the_project_add_permission(self):
        self.assertHttpStatus(self.client.get(self.url('migration')), 403)
        self.grant('add')
        self.assertHttpStatus(self.client.get(self.url('migration')), 200)

    def test_the_page_queues_nothing(self):
        self.grant('add')
        self.client.get(self.url('migration'))
        self.inventory.assert_not_called()
        self.staging.assert_not_called()

    def test_the_page_names_the_latest_run_of_each(self):
        self.grant('add')
        inventory = self.record(MigrationInventoryJob)
        response = self.client.get(self.url('migration'))
        body = response.content.decode()
        self.assertIn(inventory.get_absolute_url(), body)
        # Staging has never run, so its row says so rather than linking anywhere.
        self.assertIn('Never run', body)

    def with_findings(self, *findings):
        """Record an inventory Job carrying the supplied findings."""
        job = self.record(MigrationInventoryJob)
        job.data = {'findings': list(findings)}
        job.save()
        return job

    @staticmethod
    def finding(level, code, path):
        return {'level': level, 'code': code, 'pk': 1, 'path': path, 'message': f'{path} is {code}.'}

    def test_a_blocking_finding_is_named_on_the_page(self):
        self.grant('add')
        self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('Staging will refuse', body)
        self.assertIn('audit/legacy_report.py', body)
        self.assertIn('report_style', body)
        self.assertIn('audit/legacy_report.py is report_style.', body)

    def test_every_blocking_finding_is_listed(self):
        self.grant('add')
        self.with_findings(
            self.finding('blocking', 'report_style', 'one.py'),
            self.finding('blocking', 'not_importable', 'two-hyphen.py'),
        )
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('one.py', body)
        self.assertIn('two-hyphen.py', body)

    def test_a_warning_is_counted_rather_than_listed(self):
        self.grant('add')
        self.with_findings(
            self.finding('warning', 'legacy_import', 'first.py'),
            self.finding('warning', 'legacy_import', 'second.py'),
        )
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)
        self.assertIn('2 modules import the legacy authoring API', body)
        # Deliberate: listing the paths is the inventory's business, not this page's.
        self.assertNotIn('first.py', body)

    def test_a_clean_inventory_shows_no_refusal(self):
        self.grant('add')
        self.record(MigrationInventoryJob)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)

    def test_a_job_carrying_no_report_renders(self):
        # An inventory that failed before it recorded anything leaves data as None.
        self.grant('add')
        job = self.record(MigrationInventoryJob, status=JobStatusChoices.STATUS_ERRORED)
        job.data = None
        job.save()
        self.assertHttpStatus(self.client.get(self.url('migration')), 200)

    def inventoried(self, *keys):
        """Record an inventory Job proposing the named Projects, creating none of them."""
        job = self.record(MigrationInventoryJob)
        job.data = {'projects': [{'key': key, 'name': key.replace('-', ' ')} for key in keys]}
        job.save()
        return job

    def serving(self, *keys, activate=True):
        """Create one Project per key, serving a revision unless activate is False."""
        for key in keys:
            project = CustomScriptProject.objects.create(name=key, key=key)
            revision = ScriptProjectRevision.objects.create(
                project=project, digest='a' * 64, status=RevisionStatusChoices.ACTIVE
            )
            if not activate:
                continue
            # Set past clean(), which refuses a pointer the activation service did not move.
            CustomScriptProject.objects.filter(pk=project.pk).update(active_revision=revision)

    def page_query_count(self):
        """Render the Migration page and report how many queries it took."""
        with CaptureQueriesContext(connection) as captured:
            self.assertHttpStatus(self.client.get(self.url('migration')), 200)
        return len(captured)

    def test_the_page_does_not_query_once_per_project_row(self):
        # view as well: a row whose Project restrict() filters out never reaches the join at all.
        self.grant('add', 'view')
        first = [f'project-{index}' for index in range(2)]
        self.serving(*first)
        self.inventoried(*first)
        two = self.page_query_count()

        rest = [f'project-{index}' for index in range(2, 10)]
        self.serving(*rest)
        self.inventoried(*first, *rest)

        self.assertEqual(self.page_query_count(), two)

    def test_the_page_does_not_query_per_row_before_activation(self):
        # Between staging and activation every Project has a null pointer, which is the state the
        # page spends most of its life in, and current_revision then falls back to a query.
        self.grant('add', 'view')
        first = [f'pending-{index}' for index in range(2)]
        self.serving(*first, activate=False)
        self.inventoried(*first)
        two = self.page_query_count()

        rest = [f'pending-{index}' for index in range(2, 10)]
        self.serving(*rest, activate=False)
        self.inventoried(*first, *rest)

        self.assertEqual(self.page_query_count(), two)

    def staged(self, status, recorded_status=None, scripts=()):
        """Create a Project with a revision, and the staging Job that reports having made it."""
        project = CustomScriptProject.objects.create(name='Staged Project', key='staged-project')
        revision = ScriptProjectRevision.objects.create(
            project=project, digest='f' * 64, status=status, discovered_scripts=list(scripts)
        )
        job = self.record(MigrationStagingJob)
        job.data = {
            'projects': [
                {
                    'key': project.key,
                    'created': True,
                    'revision_pk': revision.pk,
                    'revision_created': True,
                    'revision_status': recorded_status or status,
                }
            ]
        }
        job.save()
        return project, revision

    def test_a_staged_project_shows_its_revision_status_and_script_count(self):
        self.grant('add', 'view')
        project, revision = self.staged(RevisionStatusChoices.ACTIVE, scripts=[{'class_name': 'NewIP'}])
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn(project.get_absolute_url(), body)
        self.assertIn(revision.get_absolute_url(), body)
        self.assertIn('f' * 12, body)
        self.assertIn('Active', body)

    def test_the_status_shown_is_the_verdict_not_what_staging_recorded(self):
        # The whole reason this reads live: staging records a status before validation runs.
        self.grant('add', 'view')
        self.staged(RevisionStatusChoices.INVALID, recorded_status=RevisionStatusChoices.MATERIALIZED)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('Invalid', body)
        self.assertNotIn('Materialized', body)

    def test_a_project_the_inventory_proposed_but_staging_has_not_made_is_listed(self):
        # Running the inventory without staging, or staging an older plan, both look like this.
        self.grant('add', 'view')
        self.inventoried('not-staged-yet')
        response = self.client.get(self.url('migration'))
        self.assertEqual([row['key'] for row in response.context['rows']], ['not-staged-yet'])
        self.assertIsNone(response.context['rows'][0]['project'])
        self.assertIn('Not staged', response.content.decode())

    def test_both_passes_contribute_rows_without_duplicating_one(self):
        self.grant('add', 'view')
        project, _revision = self.staged(RevisionStatusChoices.VALID)
        self.inventoried(project.key, 'not-staged-yet')
        rows = self.client.get(self.url('migration')).context['rows']
        self.assertEqual([row['key'] for row in rows], [project.key, 'not-staged-yet'])

    def test_the_table_is_absent_before_either_pass_has_run(self):
        self.grant('add', 'view')
        response = self.client.get(self.url('migration'))
        self.assertEqual(list(response.context['rows']), [])
        self.assertNotIn('Not staged', response.content.decode())

    def test_a_project_the_user_cannot_view_is_not_linked(self):
        # Gated by the project's own view permission, like every other revision surface.
        self.grant('add')
        project, _revision = self.staged(RevisionStatusChoices.INVALID)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn(project.get_absolute_url(), body)

    def test_running_the_inventory_queues_one_pass(self):
        self.grant('add')
        response = self.client.post(self.url('migration_inventory'))
        self.assertHttpStatus(response, 302)
        self.inventory.assert_called_once_with(user=self.user)
        # Back to the page, which names the run it just queued and links to it. The operator lands
        # where the state table and the other pass are, rather than one click away from both.
        self.assertEqual(response.url, self.url('migration'))

    def test_the_inventory_route_needs_the_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_inventory')), 403)
        self.inventory.assert_not_called()

    def test_the_staging_confirmation_queues_nothing(self):
        self.grant('add')
        response = self.client.get(self.url('migration_stage'))
        self.assertHttpStatus(response, 200)
        self.assertIn('Stage Projects', response.content.decode())
        self.staging.assert_not_called()

    def test_staging_queues_one_pass(self):
        self.grant('add')
        response = self.client.post(self.url('migration_stage'))
        self.assertHttpStatus(response, 302)
        self.staging.assert_called_once_with(user=self.user)
        self.assertEqual(response.url, self.url('migration'))

    def test_both_passes_return_to_the_same_place(self):
        # The already-queued refusal always came back here. Asserting the three together is what
        # stops one of them drifting off on its own.
        self.grant('add')
        queued = self.client.post(self.url('migration_inventory'))
        staged = self.client.post(self.url('migration_stage'))

        self.assertEqual(queued.url, self.url('migration'))
        self.assertEqual(staged.url, self.url('migration'))

    def test_staging_refuses_while_a_pass_is_already_queued(self):
        self.grant('add')
        self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_stage'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.staging.assert_not_called()

    def test_the_staging_route_needs_the_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_stage')), 403)
        self.staging.assert_not_called()

    def open_run(self, state):
        """Open a migration run in one state, as a pass would have left it."""
        return MigrationRun.objects.create(state=state)

    def test_the_cutover_button_appears_only_once_a_run_is_staged(self):
        self.grant('add', 'migrate')
        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        self.open_run(MigrationStateChoices.STAGING)

        self.assertIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_button_names_what_it_would_refuse(self):
        # Withholding the button without saying why is the silence this page exists to remove.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)
        blocked = [{'project_key': 'automation', 'reason': 'holds no revision'}]

        with mock.patch.object(cutover, 'unservable_projects', return_value=blocked) as guard:
            body = self.client.get(self.url('migration')).content.decode()

        guard.assert_called_once()
        self.assertNotIn('Enter cutover', body)
        self.assertIn('automation', body)
        self.assertIn('holds no revision', body)

    def test_the_page_renders_a_reason_the_guard_itself_produced(self):
        # The whole path rather than a stand-in: a real built-in module, the real guard, the
        # real template.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)
        self.legacy_module()

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Enter cutover', body)
        self.assertIn('has not been staged', body)

    def legacy_module(self):
        """Create one built-in script module, which is what gives the map a Project key to name."""
        scripts_root = tempfile.mkdtemp(prefix='legacy-scripts-')
        self.addCleanup(shutil.rmtree, scripts_root, ignore_errors=True)
        self.enterContext(
            override_settings(
                STORAGES={
                    **settings.STORAGES,
                    'scripts': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': scripts_root, 'allow_overwrite': True},
                    },
                }
            )
        )
        storages['scripts'].save('audit.py', ContentFile(b'VALUE = 1\n'))
        return ScriptModule.objects.create(file_path='audit.py')

    def staged_fence(self):
        """Return a run holding a real frozen map with activation recorded, as a fence leaves it."""
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.legacy_module()
        run.journal['mapping'] = mapping.build_map()
        # The fence refuses unless every mapped Project was staged, so one that crossed leaves
        # the rows behind, serving nothing until an activation succeeds.
        for key in mapping.project_keys(run.journal['mapping']):
            CustomScriptProject.objects.create(name=key, key=key, source_type=ProjectSourceTypeChoices.UPLOAD)
        run.record_step(cutover.STEP, counts={})
        run.record_step(cutover.ACTIVATE_STEP, projects=[])
        return run

    def test_the_references_button_names_what_it_would_refuse(self):
        # Withholding it without saying why reproduces the silence this page exists to remove.
        self.grant('add', 'migrate')
        self.staged_fence()

        with mock.patch.object(cutover, 'projects_not_serving', return_value=['automation']) as guard:
            body = self.client.get(self.url('migration')).content.decode()

        guard.assert_called_once()
        self.assertNotIn('Repoint references', body)
        self.assertIn('automation', body)

    def test_the_references_button_is_withheld_by_the_guard_itself(self):
        # The whole path: a real map, the real predicate, the real template.
        self.grant('add', 'migrate')
        run = self.staged_fence()
        expected = mapping.project_keys(run.journal['mapping'])

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Repoint references', body)
        for key in expected:
            self.assertIn(key, body)

    def test_the_references_button_returns_once_a_refused_project_is_deleted(self):
        # Deleting it is the operator saying they do not want it, which must not wedge the rest.
        self.grant('add', 'migrate')
        run = self.staged_fence()
        self.assertNotIn('Repoint references', self.client.get(self.url('migration')).content.decode())

        CustomScriptProject.objects.filter(key__in=mapping.project_keys(run.journal['mapping'])).delete()

        self.assertIn('Repoint references', self.client.get(self.url('migration')).content.decode())

    def test_the_references_button_returns_once_every_project_serves(self):
        self.grant('add', 'migrate')
        self.staged_fence()

        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            body = self.client.get(self.url('migration')).content.decode()

        self.assertIn('Repoint references', body)

    def test_the_alert_is_silent_until_activation_has_run(self):
        # Nothing serves yet and the page already offers Activate Projects, so naming them
        # here would tell the operator to redo a step they have not taken.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.legacy_module()
        run.journal['mapping'] = mapping.build_map()
        run.record_step(cutover.STEP, counts={})

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('The reference pass will refuse', body)
        self.assertIn('Activate Projects', body)

    def test_the_page_does_not_ask_before_the_fence_has_captured(self):
        # The predicate reads the frozen map, so asking without one would raise rather than refuse.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER)

        with mock.patch.object(cutover, 'projects_not_serving') as guard:
            self.client.get(self.url('migration'))

        guard.assert_not_called()

    def test_the_page_does_not_ask_what_a_crossed_fence_would_refuse(self):
        # The check reads the built-in rows, so it runs only where the button could render. That
        # is the recorded step rather than the state, since the button stays up mid-crossing.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER).record_step(cutover.STEP, counts={})

        with mock.patch.object(cutover, 'unservable_projects') as guard:
            self.client.get(self.url('migration'))

        guard.assert_not_called()

    def test_the_cutover_button_is_gone_once_the_fence_has_been_crossed(self):
        # The page must not offer a step the job would return from having done nothing.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER).record_step(cutover.STEP, counts={})

        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_is_still_offered_to_a_run_the_fence_left_mid_crossing(self):
        # Withholding the button here would strand the operator: staging refuses this state and
        # activation waits on the step record.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER)

        body = self.client.get(self.url('migration')).content.decode()

        self.assertIn('Enter cutover', body)
        self.assertNotIn('Activate Projects', body)

    def test_the_page_says_the_fence_may_have_fired(self):
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)

        self.assertIn('may already have closed', self.client.get(self.url('migration')).content.decode())

        run.record_step(cutover.STEP, counts={})

        self.assertNotIn('may already have closed', self.client.get(self.url('migration')).content.decode())

    def test_a_staged_run_is_not_described_as_mid_crossing(self):
        # The other half of the predicate: without it the notice would greet every staged run.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)

        self.assertNotIn('may already have closed', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_confirmation_queues_nothing(self):
        self.grant('add', 'migrate')
        response = self.client.get(self.url('migration_cutover'))
        self.assertHttpStatus(response, 200)
        self.assertIn('no way back', response.content.decode())
        self.cutover.assert_not_called()

    def test_the_cutover_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_cutover'), {'confirm': 'true', 'backup_taken': 'on'})
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.cutover.assert_called_once()

    def test_the_cutover_refuses_a_post_that_acknowledges_nothing(self):
        # Posted as a browser would, with the form's own marker but the box left unticked, so the
        # refusal is the acknowledgement and not ConfirmationForm's hidden field.
        self.grant('add', 'migrate')

        response = self.client.post(self.url('migration_cutover'), {'confirm': 'true'})

        self.assertHttpStatus(response, 200)
        self.cutover.assert_not_called()

    def test_the_cutover_confirmation_asks_for_the_backup(self):
        self.grant('add', 'migrate')

        body = self.client.get(self.url('migration_cutover')).content.decode()

        self.assertIn('backup_taken', body)
        self.assertIn('no way back', body)

    def test_the_cutover_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationCutoverJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_cutover'))
        self.assertHttpStatus(response, 302)
        self.cutover.assert_not_called()

    def test_the_cutover_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_cutover')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_cutover')), 403)
        self.cutover.assert_not_called()

    def test_the_page_names_the_latest_cutover(self):
        self.grant('add')
        job = self.record(MigrationCutoverJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_a_staging_user_is_offered_none_of_the_steps_past_the_fence(self):
        # Every render condition is satisfied at each point, so the permission is all that keeps
        # them off. The crossing and the steps after it cannot render at once, so it takes two.
        self.grant('add')
        run = self.open_run(MigrationStateChoices.STAGING)

        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})
        run.record_step('activate', projects=[])
        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Activate Projects', body)
        self.assertNotIn('Repoint references', body)

    def test_a_migrating_user_is_offered_every_step_the_state_allows(self):
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.STAGING)

        self.assertIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})
        run.record_step('activate', projects=[])
        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Enter cutover', body)
        self.assertIn('Activate Projects', body)
        self.assertIn('Repoint references', body)

    def test_the_activate_button_appears_only_once_the_fence_is_recorded(self):
        # Crossing the fence is what activation waits for, not merely reaching the cutover state:
        # a run whose cutover job failed partway has the state and not the step.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Activate Projects', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})

        self.assertIn('Activate Projects', self.client.get(self.url('migration')).content.decode())

    def test_the_activate_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_activate'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.activation.assert_called_once()

    def test_the_activate_route_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationActivationJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_activate'))
        self.assertHttpStatus(response, 302)
        self.activation.assert_not_called()

    def test_the_activate_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_activate')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_activate')), 403)
        self.activation.assert_not_called()

    def test_the_page_names_the_latest_activation(self):
        self.grant('add')
        job = self.record(MigrationActivationJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_the_repoint_button_appears_only_once_the_projects_are_activated(self):
        # The references name plugin rows, and activation is what creates them.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Repoint references', self.client.get(self.url('migration')).content.decode())

        run.record_step('activate', projects=[])

        self.assertIn('Repoint references', self.client.get(self.url('migration')).content.decode())

    def test_the_repoint_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_repoint'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.references.assert_called_once()

    def test_the_repoint_route_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationReferencesJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_repoint'))
        self.assertHttpStatus(response, 302)
        self.references.assert_not_called()

    def test_the_repoint_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_repoint')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_repoint')), 403)
        self.references.assert_not_called()

    def test_the_page_names_the_latest_reference_pass(self):
        self.grant('add')
        job = self.record(MigrationReferencesJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_the_cleanup_button_waits_for_every_reference_step(self):
        # One finished step is not enough: a schedule needs the built-in rows still there.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history'):
            run.record_step(step, counts={})
            self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

        run.record_step('recreate_schedules', counts={})

        self.assertIn('Clean up', self.client.get(self.url('migration')).content.decode())

    def test_the_cleanup_confirmation_queues_nothing(self):
        self.grant('add', 'migrate')
        response = self.client.get(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 200)
        self.assertIn('stored source', response.content.decode())
        self.cleanup.assert_not_called()

    def test_the_cleanup_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.cleanup.assert_called_once()

    def test_the_cleanup_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationCleanupJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 302)
        self.cleanup.assert_not_called()

    def test_the_cleanup_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_cleanup')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_cleanup')), 403)
        self.cleanup.assert_not_called()

    def test_a_staging_user_is_not_offered_the_cleanup(self):
        self.grant('add')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history', 'recreate_schedules'):
            run.record_step(step, counts={})

        self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

    def test_the_page_names_the_latest_cleanup(self):
        self.grant('add')
        job = self.record(MigrationCleanupJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_the_verify_button_is_offered_at_every_state(self):
        # It reads only, so unlike every other pass it waits on nothing.
        self.grant('add')

        self.assertIn('Verify', self.client.get(self.url('migration')).content.decode())

    def test_the_verify_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add')
        response = self.client.post(self.url('migration_verify'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.verification.assert_called_once()

    def test_the_verify_route_takes_the_staging_permission_not_the_migrate_one(self):
        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 302)
        self.verification.assert_called_once()

    def test_the_verify_route_is_never_refused_for_being_queued(self):
        # It writes nothing, so two at once race on nothing.
        self.grant('add')
        self.record(MigrationVerificationJob, status=JobStatusChoices.STATUS_RUNNING)

        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 302)
        self.verification.assert_called_once()

    def test_the_page_names_the_latest_verification(self):
        self.grant('add')
        job = self.record(MigrationVerificationJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())
