from rest_framework import status

from netbox_custom_scripts.choices import ModuleDiscoveryStatusChoices, RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.tests.plugin_testing import PluginAPIViewTestCases

DIGEST = 'c' * 64


class CustomScriptModuleAPIViewTestCase(PluginAPIViewTestCases.APIViewTestCase):
    model = CustomScriptModule
    brief_fields = ['description', 'display', 'id', 'source_path', 'url']
    graphql_filter = {'source_path': {'lookup': 'i_contains', 'value': 'tools'}}
    # The default update_data falls back to create_data[0], which carries the frozen
    # project and source_path fields.
    update_data = {
        'description': 'Updated description',
        'enabled': False,
    }
    bulk_update_data = {
        'description': 'Bulk-updated description',
        'enabled': False,
    }

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='API Module Project', key='api-module-project')

        CustomScriptModule.objects.create(project=cls.project, source_path='tools/first.py', description='First module')
        CustomScriptModule.objects.create(
            project=cls.project, source_path='tools/second.py', description='Second module'
        )
        CustomScriptModule.objects.create(project=cls.project, source_path='tools/third.py', enabled=False)

        cls.create_data = [
            {'project': cls.project.pk, 'source_path': 'tools/fourth.py', 'description': 'Fourth module'},
            {'project': cls.project.pk, 'source_path': 'tools/fifth.py', 'description': 'Fifth module'},
            {'project': cls.project.pk, 'source_path': 'tools/sixth.py', 'description': '', 'enabled': False},
        ]

    def test_discovery_fields_are_read_only(self):
        self.add_permissions('netbox_custom_scripts.change_customscriptmodule')
        module = CustomScriptModule.objects.create(project=self.project, source_path='tools/system.py')
        revision = CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        response = self.client.patch(
            self._get_detail_url(module),
            {
                'discovery_status': ModuleDiscoveryStatusChoices.DISCOVERED,
                'discovery_error': 'Injected error',
                'last_discovered_revision': revision.pk,
            },
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        module.refresh_from_db()
        self.assertEqual(module.discovery_status, ModuleDiscoveryStatusChoices.PENDING)
        self.assertEqual(module.discovery_error, '')
        self.assertIsNone(module.last_discovered_revision)

    def test_source_path_is_immutable(self):
        self.add_permissions('netbox_custom_scripts.change_customscriptmodule')
        module = CustomScriptModule.objects.create(project=self.project, source_path='tools/frozen.py')
        response = self.client.patch(
            self._get_detail_url(module), {'source_path': 'tools/renamed.py'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_path', response.data)
        module.refresh_from_db()
        self.assertEqual(module.source_path, 'tools/frozen.py')

    def test_project_is_immutable(self):
        self.add_permissions('netbox_custom_scripts.change_customscriptmodule')
        other = CustomScriptProject.objects.create(name='API Other Project', key='api-other-project')
        module = CustomScriptModule.objects.create(project=self.project, source_path='tools/owned.py')
        response = self.client.patch(self._get_detail_url(module), {'project': other.pk}, format='json', **self.header)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('project', response.data)

    def test_source_path_is_canonicalized_on_creation(self):
        self.add_permissions('netbox_custom_scripts.add_customscriptmodule')
        response = self.client.post(
            self._get_list_url(),
            {'project': self.project.pk, 'source_path': './tools//created.py'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['source_path'], 'tools/created.py')

    def test_creation_rejects_an_unimportable_path(self):
        self.add_permissions('netbox_custom_scripts.add_customscriptmodule')
        response = self.client.post(
            self._get_list_url(),
            {'project': self.project.pk, 'source_path': 'tools/deploy.txt'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_path', response.data)

    def test_creation_rejects_traversal(self):
        self.add_permissions('netbox_custom_scripts.add_customscriptmodule')
        response = self.client.post(
            self._get_list_url(),
            {'project': self.project.pk, 'source_path': '../outside.py'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_path', response.data)

    def test_case_folded_sibling_is_rejected(self):
        self.add_permissions('netbox_custom_scripts.add_customscriptmodule')
        response = self.client.post(
            self._get_list_url(),
            {'project': self.project.pk, 'source_path': 'Tools/First.py'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_path', response.data)
