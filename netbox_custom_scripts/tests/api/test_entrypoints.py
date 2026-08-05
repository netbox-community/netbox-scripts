from unittest import mock

from django.urls import reverse
from rest_framework import status

from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.jobs import ProjectEntrypointRefreshJob
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from utilities.testing import APITestCase


class EntrypointsAPITestCase(APITestCase):
    """Entrypoint selection is a project operation, not free-text Module creation."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='API Tab Project', key='api-tab-project')
        CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            manifest=[
                {'path': path, 'size': 1, 'sha256': 'a' * 64} for path in ('deploy.py', 'tools/audit.py', 'notes.md')
            ],
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def allow_writes(self):
        # A PUT on the project viewset needs the project's change permission from NetBox's
        # token permissions, and the declarations' own on top.
        self.add_permissions(
            'netbox_custom_scripts.view_customscriptproject',
            'netbox_custom_scripts.change_customscriptproject',
            'netbox_custom_scripts.change_customscriptmodule',
        )

    def url(self):
        return reverse('plugins-api:netbox_custom_scripts-api:customscriptproject-entrypoints', args=[self.project.pk])

    def test_get_reports_the_candidate_inventory(self):
        self.add_permissions('netbox_custom_scripts.view_customscriptproject')
        response = self.client.get(self.url(), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['mode'], 'ui')
        self.assertEqual(
            [entry['path'] for entry in response.data['candidates']],
            ['deploy.py', 'tools/audit.py'],
        )
        self.assertFalse(any(entry['selected'] for entry in response.data['candidates']))

    def test_put_replaces_the_selection(self):
        self.allow_writes()
        response = self.client.put(self.url(), {'paths': ['tools/audit.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            list(self.project.modules.filter(enabled=True).values_list('source_path', flat=True)),
            ['tools/audit.py'],
        )
        selected = {entry['path']: entry['selected'] for entry in response.data['candidates']}
        self.assertTrue(selected['tools/audit.py'])
        self.assertFalse(selected['deploy.py'])

    def test_put_disables_rather_than_deletes(self):
        self.allow_writes()
        module = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        response = self.client.put(self.url(), {'paths': []}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        module.refresh_from_db()
        self.assertFalse(module.enabled)

    def test_put_refuses_a_path_outside_the_source(self):
        self.allow_writes()
        response = self.client.put(self.url(), {'paths': ['nowhere.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_refuses_a_malformed_payload(self):
        self.allow_writes()
        response = self.client.put(self.url(), {'paths': 'deploy.py'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_needs_the_module_permission(self):
        self.add_permissions(
            'netbox_custom_scripts.view_customscriptproject',
            'netbox_custom_scripts.change_customscriptproject',
        )
        response = self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.project.modules.exists())

    def test_a_put_that_changes_the_selection_enqueues_a_refresh(self):
        # The tab and the API must not disagree about what saving a selection does, so both
        # apply it to the stored source rather than only writing the declarations.
        self.allow_writes()
        with mock.patch.object(ProjectEntrypointRefreshJob, 'enqueue_refresh') as enqueue:
            self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0].pk, self.project.pk)

    def test_a_put_that_changes_nothing_enqueues_no_refresh(self):
        self.allow_writes()
        self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        with mock.patch.object(ProjectEntrypointRefreshJob, 'enqueue_refresh') as enqueue:
            self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        enqueue.assert_not_called()

    def test_a_declared_path_missing_from_the_source_is_reported_unavailable(self):
        self.add_permissions('netbox_custom_scripts.view_customscriptproject')
        CustomScriptModule.objects.create(project=self.project, source_path='removed.py')
        response = self.client.get(self.url(), **self.header)
        entry = next(item for item in response.data['candidates'] if item['path'] == 'removed.py')
        self.assertFalse(entry['available'])
        self.assertTrue(entry['selected'])
