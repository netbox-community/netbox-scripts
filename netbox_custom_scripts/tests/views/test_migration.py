import uuid
from unittest import mock

from django.urls import reverse

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from netbox_custom_scripts.jobs import MigrationInventoryJob, MigrationStagingJob
from netbox_custom_scripts.models import CustomScriptProject
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

    def test_running_the_inventory_queues_one_pass(self):
        self.grant('add')
        response = self.client.post(self.url('migration_inventory'))
        self.assertHttpStatus(response, 302)
        self.inventory.assert_called_once_with(user=self.user)
        self.assertEqual(response.url, Job.objects.get(name='queued').get_absolute_url())

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
