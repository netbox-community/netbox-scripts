from unittest import mock

from django.urls import reverse
from rest_framework import status

from core.models import ObjectType
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.jobs import ProjectScriptFileRefreshJob
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from users.models import ObjectPermission
from utilities.testing import APITestCase


class ScriptFilesAPITestCase(APITestCase):
    """Selecting script files is a project operation, not free-text row creation."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='API Tab Project', key='api-tab-project')
        ScriptProjectRevision.objects.create(
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
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.change_scriptfile',
            'netbox_scripts.add_scriptfile',
        )

    def url(self):
        return reverse('plugins-api:netbox_scripts-api:scriptproject-script-files', args=[self.project.pk])

    def test_get_reports_the_candidate_inventory(self):
        self.add_permissions('netbox_scripts.view_scriptproject')
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
            list(self.project.script_files.filter(enabled=True).values_list('source_path', flat=True)),
            ['tools/audit.py'],
        )
        selected = {entry['path']: entry['selected'] for entry in response.data['candidates']}
        self.assertTrue(selected['tools/audit.py'])
        self.assertFalse(selected['deploy.py'])

    def test_put_disables_rather_than_deletes(self):
        self.allow_writes()
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        response = self.client.put(self.url(), {'paths': []}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        script_file.refresh_from_db()
        self.assertFalse(script_file.enabled)

    def test_put_refuses_a_path_outside_the_source(self):
        self.allow_writes()
        response = self.client.put(self.url(), {'paths': ['nowhere.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_refuses_a_malformed_payload(self):
        self.allow_writes()
        response = self.client.put(self.url(), {'paths': 'deploy.py'}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_needs_the_script_file_permission(self):
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
        )
        response = self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.project.script_files.exists())

    def test_a_put_that_changes_the_selection_enqueues_a_refresh(self):
        # The tab and the API must not disagree about what saving a selection does, so both
        # apply it to the stored source rather than only writing the declarations.
        self.allow_writes()
        with mock.patch.object(ProjectScriptFileRefreshJob, 'enqueue_refresh') as enqueue:
            self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        self.assertEqual(enqueue.call_count, 1)
        self.assertEqual(enqueue.call_args.args[0].pk, self.project.pk)

    def test_a_put_that_changes_nothing_enqueues_no_refresh(self):
        self.allow_writes()
        self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        with mock.patch.object(ProjectScriptFileRefreshJob, 'enqueue_refresh') as enqueue:
            self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        enqueue.assert_not_called()

    def test_a_declared_path_missing_from_the_source_is_reported_unavailable(self):
        self.add_permissions('netbox_scripts.view_scriptproject')
        ScriptFile.objects.create(project=self.project, source_path='removed.py')
        response = self.client.get(self.url(), **self.header)
        entry = next(item for item in response.data['candidates'] if item['path'] == 'removed.py')
        self.assertFalse(entry['available'])
        self.assertTrue(entry['selected'])

    def test_a_selection_cannot_create_on_change_permission_alone(self):
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.change_scriptfile',
        )
        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.client.put(self.url(), {'paths': ['deploy.py']}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.project.script_files.exists())
        flush.assert_not_called()

    def test_changing_existing_declarations_does_not_require_add(self):
        item = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.change_scriptfile',
        )
        response = self.client.put(self.url(), {'paths': []}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        item.refresh_from_db()
        self.assertFalse(item.enabled)

    def test_a_scoped_selection_refuses_the_whole_change(self):
        allowed = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        denied = ScriptFile.objects.create(project=self.project, source_path='tools/audit.py')
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        permission = ObjectPermission.objects.create(
            name='One declaration', actions=['change'], constraints={'pk': allowed.pk}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(ScriptFile))
        permission.users.add(self.user)
        response = self.client.put(self.url(), {'paths': []}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        allowed.refresh_from_db()
        denied.refresh_from_db()
        self.assertTrue(allowed.enabled)
        self.assertTrue(denied.enabled)
