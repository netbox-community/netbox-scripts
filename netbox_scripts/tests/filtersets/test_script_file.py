from django.test import TestCase

from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.filtersets import ScriptFileFilterSet
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ChangeLoggedFilterSetTestMixin

DIGEST = 'b' * 64


class ScriptFileFilterSetTestCase(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = ScriptFile.objects.all()
    filterset = ScriptFileFilterSet

    @classmethod
    def setUpTestData(cls):
        cls.projects = (
            ScriptProject(name='Alpha', key='alpha'),
            ScriptProject(name='Bravo', key='bravo'),
        )
        for project in cls.projects:
            project.save()

        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.projects[0],
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )

        modules = (
            ScriptFile(
                project=cls.projects[0],
                source_path='deploy.py',
                description='Rolls out configuration',
            ),
            ScriptFile(
                project=cls.projects[0],
                source_path='tools/audit.py',
                description='Reads inventory',
                discovery_status=FileDiscoveryStatusChoices.DISCOVERED,
                last_discovered_revision=cls.revision,
            ),
            ScriptFile(
                project=cls.projects[0],
                source_path='tools/report.py',
                description='Summarizes results',
                enabled=False,
                discovery_status=FileDiscoveryStatusChoices.FAILED,
                discovery_error='The entrypoint could not be imported.',
            ),
            ScriptFile(
                project=cls.projects[1],
                source_path='deploy.py',
                description='Rolls out configuration',
                discovery_status=FileDiscoveryStatusChoices.DISCOVERED,
            ),
            ScriptFile(
                project=cls.projects[1],
                source_path='tools/audit.py',
                description='Reads inventory',
                enabled=False,
            ),
            ScriptFile(
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
        params = {'discovery_status': [FileDiscoveryStatusChoices.DISCOVERED]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)
        params = {
            'discovery_status': [
                FileDiscoveryStatusChoices.PENDING,
                FileDiscoveryStatusChoices.FAILED,
            ]
        }
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 4)

    def test_discovery_error(self):
        params = {'discovery_error': 'The entrypoint could not be imported.'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_last_discovered_revision(self):
        params = {'last_discovered_revision_id': [self.revision.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
