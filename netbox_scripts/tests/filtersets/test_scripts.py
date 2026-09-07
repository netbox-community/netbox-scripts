from django.test import TestCase

from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.filtersets import NetBoxScriptFilterSet, ScriptFileFilterSet
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ChangeLoggedFilterSetTestMixin

DIGEST = 'c' * 64


class NetBoxScriptFilterSetTestCase(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = NetBoxScript.objects.all()
    filterset = NetBoxScriptFilterSet
    # metadata holds execution defaults read from the class, not a lookup key.
    ignore_fields = ('metadata',)

    @classmethod
    def setUpTestData(cls):
        cls.projects = (
            ScriptProject(name='Filter Alpha', key='filter-alpha'),
            ScriptProject(name='Filter Bravo', key='filter-bravo'),
        )
        for project in cls.projects:
            project.save()

        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.projects[0],
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )

        scripts = (
            NetBoxScript(
                project=cls.projects[0],
                module_path='deploy',
                class_name='DeployDevices',
                display_name='Deploy Devices',
                description='Rolls out configuration',
                last_seen_revision=cls.revision,
                job_timeout_override=45,
                notifications_default_override='never',
            ),
            NetBoxScript(
                project=cls.projects[0],
                module_path='tools.audit',
                class_name='AuditInventory',
                display_name='Audit Inventory',
                description='Reads inventory',
                enabled=False,
                commit_default_override=False,
            ),
            NetBoxScript(
                project=cls.projects[1],
                module_path='tools.report',
                class_name='SummarizeResults',
                display_name='Summarize Results',
                description='Summarizes results',
                is_retired=True,
            ),
        )
        for script in scripts:
            script.save()

    def test_q(self):
        self.assertEqual(self.filterset({'q': 'Deploy'}, self.queryset).qs.count(), 1)
        self.assertEqual(self.filterset({'q': 'tools'}, self.queryset).qs.count(), 2)

    def test_project(self):
        params = {'project': ['filter-alpha']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_project_id(self):
        params = {'project_id': [self.projects[1].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_enabled(self):
        self.assertEqual(self.filterset({'enabled': False}, self.queryset).qs.count(), 1)

    def test_commit_default_override(self):
        self.assertEqual(self.filterset({'commit_default_override': False}, self.queryset).qs.count(), 1)

    def test_job_timeout_override(self):
        self.assertEqual(self.filterset({'job_timeout_override': [45]}, self.queryset).qs.count(), 1)

    def test_notifications_default_override(self):
        self.assertEqual(self.filterset({'notifications_default_override': ['never']}, self.queryset).qs.count(), 1)

    def test_is_retired(self):
        self.assertEqual(self.filterset({'is_retired': True}, self.queryset).qs.count(), 1)

    def test_module_path(self):
        params = {'module_path': ['tools.audit']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_class_name(self):
        params = {'class_name': ['DeployDevices']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_display_name(self):
        params = {'display_name': ['Audit Inventory']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_last_seen_revision_id(self):
        params = {'last_seen_revision_id': [self.revision.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_description(self):
        params = {'description': ['Reads inventory']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)


FILE_DIGEST = 'b' * 64


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
            digest=FILE_DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )

        script_files = (
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
                discovery_error='The script file could not be imported.',
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
        for script_file in script_files:
            script_file.save()

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
        params = {'discovery_error': 'The script file could not be imported.'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_last_discovered_revision(self):
        params = {'last_discovered_revision_id': [self.revision.pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
