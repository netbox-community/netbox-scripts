from django.test import TestCase

from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.filtersets import CustomScriptFilterSet
from netbox_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ChangeLoggedFilterSetTestMixin

DIGEST = 'c' * 64


class CustomScriptFilterSetTestCase(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = CustomScript.objects.all()
    filterset = CustomScriptFilterSet
    # metadata holds execution defaults read from the class, not a lookup key.
    ignore_fields = ('metadata',)

    @classmethod
    def setUpTestData(cls):
        cls.projects = (
            CustomScriptProject(name='Filter Alpha', key='filter-alpha'),
            CustomScriptProject(name='Filter Bravo', key='filter-bravo'),
        )
        for project in cls.projects:
            project.save()

        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.projects[0],
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )

        scripts = (
            CustomScript(
                project=cls.projects[0],
                module_path='deploy',
                class_name='DeployDevices',
                display_name='Deploy Devices',
                description='Rolls out configuration',
                last_seen_revision=cls.revision,
                job_timeout_override=45,
                notifications_default_override='never',
            ),
            CustomScript(
                project=cls.projects[0],
                module_path='tools.audit',
                class_name='AuditInventory',
                display_name='Audit Inventory',
                description='Reads inventory',
                enabled=False,
                commit_default_override=False,
            ),
            CustomScript(
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
