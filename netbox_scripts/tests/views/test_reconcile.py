from unittest import mock

from django.urls import reverse

from core.models import DataSource
from netbox_scripts.choices import ProjectSourceTypeChoices
from netbox_scripts.jobs import ProjectReconciliationJob
from netbox_scripts.models import ScriptProject
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin
from utilities.testing import TestCase, create_test_user


class ReconcileSourceViewTestCase(ObjectPermissionTestMixin, TestCase):
    """The on-demand Reconcile Source action on a Data Source-backed Project."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        self.project = ScriptProject.objects.create(
            name='Repo Project',
            key='repo-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='scripts',
        )
        self.uploaded = ScriptProject.objects.create(name='Uploaded', key='uploaded')
        self.enqueued = self.enterContext(
            mock.patch.object(ProjectReconciliationJob, 'enqueue_reconciliation', return_value=None)
        )

    def grant(self, *actions):
        return super().grant(ScriptProject, *actions)

    @staticmethod
    def url(project):
        return reverse('plugins:netbox_scripts:scriptproject_reconcile', args=[project.pk])

    def test_a_get_confirms_without_enqueueing_anything(self):
        self.grant('view', 'reconcile')
        response = self.client.get(self.url(self.project))
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertIn('Reconcile source', body)
        # The confirmation names the directory the source would be rebuilt from.
        self.assertIn('scripts', body)
        self.enqueued.assert_not_called()

    def test_a_post_enqueues_one_reconciliation_and_redirects(self):
        self.grant('view', 'reconcile')
        response = self.client.post(self.url(self.project))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.project.get_absolute_url())
        self.enqueued.assert_called_once()
        self.assertEqual(self.enqueued.call_args.args[0].pk, self.project.pk)

    def test_the_reconcile_permission_is_required(self):
        self.grant('view')
        self.assertHttpStatus(self.client.post(self.url(self.project)), 403)
        self.enqueued.assert_not_called()

    def test_the_route_does_not_apply_to_an_upload_project(self):
        # Narrowed by queryset rather than refused in the handler, because a project whose source
        # is uploaded has no directory this action could reconcile against.
        self.grant('view', 'reconcile')
        self.assertHttpStatus(self.client.get(self.url(self.uploaded)), 404)
        self.assertHttpStatus(self.client.post(self.url(self.uploaded)), 404)
        self.enqueued.assert_not_called()

    def test_the_button_is_offered_on_a_data_source_project(self):
        self.grant('view', 'reconcile')
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(f'href="{self.url(self.project)}"', body)

    def test_the_button_is_absent_on_an_upload_project(self):
        self.grant('view', 'reconcile')
        body = self.client.get(self.uploaded.get_absolute_url()).content.decode()
        self.assertNotIn(self.url(self.uploaded), body)

    def test_the_button_is_absent_without_the_reconcile_permission(self):
        self.grant('view')
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertNotIn(self.url(self.project), body)
