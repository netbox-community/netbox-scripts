from django.test import TestCase

from netbox_scripts.choices import ModuleDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.filtersets import CustomScriptModuleFilterSet
from netbox_scripts.models import CustomScriptModule, CustomScriptProject, ScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ChangeLoggedFilterSetTestMixin

DIGEST = 'b' * 64


class CustomScriptModuleFilterSetTestCase(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = CustomScriptModule.objects.all()
    filterset = CustomScriptModuleFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.projects = (
            CustomScriptProject(name='Alpha', key='alpha'),
            CustomScriptProject(name='Bravo', key='bravo'),
        )
        for project in cls.projects:
            project.save()

        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.projects[0],
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )

        modules = (
            CustomScriptModule(
                project=cls.projects[0],
                source_path='deploy.py',
                description='Rolls out configuration',
            ),
            CustomScriptModule(
                project=cls.projects[0],
                source_path='tools/audit.py',
                description='Reads inventory',
                discovery_status=ModuleDiscoveryStatusChoices.DISCOVERED,
                last_discovered_revision=cls.revision,
            ),
            CustomScriptModule(
                project=cls.projects[0],
                source_path='tools/report.py',
                description='Summarizes results',
                enabled=False,
                discovery_status=ModuleDiscoveryStatusChoices.FAILED,
                discovery_error='The entrypoint could not be imported.',
            ),
            CustomScriptModule(
                project=cls.projects[1],
                source_path='deploy.py',
                description='Rolls out configuration',
                discovery_status=ModuleDiscoveryStatusChoices.DISCOVERED,
            ),
            CustomScriptModule(
                project=cls.projects[1],
                source_path='tools/audit.py',
                description='Reads inventory',
                enabled=False,
            ),
            CustomScriptModule(
                project=cls.projects[1],
                source_path='sync.py',
                description='Refreshes cached state',
            ),
        )
        for module in modules:
            module.save()

    def test_q(self):
        params = {'q': 'Summarizes'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
        params = {'q': 'tools/audit'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'q': 'alpha'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_source_path(self):
        params = {'source_path': ['deploy.py']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {'source_path': ['sync.py', 'tools/report.py']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_project(self):
        params = {'project_id': [self.projects[0].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)
        params = {'project': [self.projects[1].key]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 3)

    def test_enabled(self):
        params = {'enabled': True}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 4)
        params = {'enabled': False}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_discovery_status(self):
        params = {'discovery_status': [ModuleDiscoveryStatusChoices.DISCOVERED]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {
            'discovery_status': [
                ModuleDiscoveryStatusChoices.PENDING,
                ModuleDiscoveryStatusChoices.FAILED,
            ]
        }
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 4)

    def test_discovery_error(self):
        params = {'discovery_error': 'The entrypoint could not be imported.'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_last_discovered_revision(self):
        params = {'last_discovered_revision_id': [self.revision.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
