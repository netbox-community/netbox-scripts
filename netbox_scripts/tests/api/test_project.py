import uuid

from rest_framework import status

from core.models import DataSource
from netbox_scripts.choices import ProjectSourceTypeChoices
from netbox_scripts.models import CustomScriptProject
from netbox_scripts.tests.plugin_testing import PluginAPIViewTestCases


class CustomScriptProjectAPIViewTestCase(PluginAPIViewTestCases.APIViewTestCase):
    model = CustomScriptProject
    brief_fields = ['description', 'display', 'id', 'key', 'name', 'url']
    # Explicit update_data: the default falls back to create_data[0], whose 'key'
    # differs from the updated instance's, and key is immutable after creation.
    update_data = {
        'name': 'CustomScriptProject 1 Updated',
        'description': 'Updated description',
        'enabled': False,
    }
    bulk_update_data = {
        'description': 'Bulk-updated description',
        'enabled': False,
    }

    @classmethod
    def setUpTestData(cls):
        CustomScriptProject.objects.create(name='CustomScriptProject 1', key='project-1', description='First project')
        CustomScriptProject.objects.create(name='CustomScriptProject 2', key='project-2', description='Second project')
        CustomScriptProject.objects.create(name='CustomScriptProject 3', key='project-3', enabled=False)

        cls.create_data = [
            {'name': 'CustomScriptProject 4', 'key': 'project-4', 'description': 'Fourth project'},
            {'name': 'CustomScriptProject 5', 'key': 'project-5', 'description': 'Fifth project'},
            {'name': 'CustomScriptProject 6', 'key': 'project-6', 'description': '', 'enabled': False},
        ]

    def test_key_is_immutable(self):
        self.add_permissions('netbox_scripts.change_customscriptproject')
        project = CustomScriptProject.objects.create(name='API Project 1', key='api-project-1')
        response = self.client.patch(
            self._get_detail_url(project), {'key': 'api-project-1-renamed'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('key', response.data)

    def test_source_type_is_immutable(self):
        self.add_permissions('netbox_scripts.change_customscriptproject')
        project = CustomScriptProject.objects.create(name='API Project 2', key='api-project-2')
        response = self.client.patch(
            self._get_detail_url(project),
            {'source_type': ProjectSourceTypeChoices.DATA_SOURCE},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_type', response.data)

    def test_data_path_rejects_traversal(self):
        self.add_permissions('netbox_scripts.change_customscriptproject')
        project = CustomScriptProject.objects.create(name='API Project 3', key='api-project-3')
        response = self.client.patch(
            self._get_detail_url(project), {'data_path': '../outside'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('data_path', response.data)

    def test_storage_key_is_read_only(self):
        self.add_permissions('netbox_scripts.change_customscriptproject')
        project = CustomScriptProject.objects.create(name='API Project 4', key='api-project-4')
        original = project.storage_key
        response = self.client.patch(
            self._get_detail_url(project), {'storage_key': str(uuid.uuid4())}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.storage_key, original)

    def test_data_path_is_canonicalized(self):
        self.add_permissions('netbox_scripts.change_customscriptproject')
        data_source = DataSource.objects.create(
            name='API Data Source 1',
            type='local',
            source_url='file:///tmp/api-data-source-1/',
        )
        project = CustomScriptProject.objects.create(
            name='API Project 5',
            key='api-project-5',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        response = self.client.patch(
            self._get_detail_url(project), {'data_path': './automation//netbox/'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'automation/netbox')

    def test_active_revision_not_in_response(self):
        self.add_permissions('netbox_scripts.view_customscriptproject')
        project = CustomScriptProject.objects.create(name='API Project 6', key='api-project-6')
        response = self.client.get(self._get_detail_url(project), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('active_revision', response.data)
