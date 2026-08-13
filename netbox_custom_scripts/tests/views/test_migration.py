import uuid
from unittest import mock

from django.urls import reverse

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.jobs import MigrationInventoryJob, MigrationStagingJob
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
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
        return reverse(f'plugins:netbox_custom_scripts:{name}')

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

    def inventoried(self, *keys):
        """Record an inventory Job proposing the named Projects, creating none of them."""
        job = self.record(MigrationInventoryJob)
        job.data = {'projects': [{'key': key, 'name': key.replace('-', ' ')} for key in keys]}
        job.save()
        return job

    def staged(self, status, recorded_status=None, scripts=()):
        """Create a Project with a revision, and the staging Job that reports having made it."""
        project = CustomScriptProject.objects.create(name='Staged Project', key='staged-project')
        revision = CustomScriptProjectRevision.objects.create(
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
