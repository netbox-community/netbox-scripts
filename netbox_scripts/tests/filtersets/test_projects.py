from django.test import TestCase

from core.models import DataSource
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_scripts.filtersets import ScriptProjectFilterSet
from netbox_scripts.models import ScriptProject
from netbox_scripts.tests.plugin_testing import ChangeLoggedFilterSetTestMixin


class ScriptProjectFilterSetTestCase(TestCase, ChangeLoggedFilterSetTestMixin):
    queryset = ScriptProject.objects.all()
    filterset = ScriptProjectFilterSet
    # storage_key is deliberately unfiltered (internal storage/runtime identity), and
    # active_revision is written only by the storage activation service.
    ignore_fields = ('storage_key', 'active_revision')

    @classmethod
    def setUpTestData(cls):
        cls.data_sources = (
            DataSource(name='Data Source 1', type='local', source_url='file:///tmp/data-source-1/'),
            DataSource(name='Data Source 2', type='local', source_url='file:///tmp/data-source-2/'),
        )
        for data_source in cls.data_sources:
            data_source.save()

        projects = (
            ScriptProject(
                name='Alpha',
                key='alpha',
                description='First entry',
            ),
            ScriptProject(
                name='Bravo',
                key='bravo',
                description='Second entry',
            ),
            ScriptProject(
                name='Charlie',
                key='charlie',
                description='Third entry',
                enabled=False,
            ),
            ScriptProject(
                name='Delta',
                key='delta',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=cls.data_sources[0],
                data_path='automation/netbox',
                activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
                enabled=False,
            ),
            ScriptProject(
                name='Echo',
                key='echo',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=cls.data_sources[1],
                data_path='scripts',
                activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
            ),
            ScriptProject(
                name='Foxtrot',
                key='foxtrot',
            ),
        )
        for project in projects:
            project.save()

    def test_q(self):
        params = {'q': 'First'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
        params = {'q': 'bravo'}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_name(self):
        params = {'name': ['Alpha', 'Bravo']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_key(self):
        params = {'key': ['alpha', 'delta']}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_source_type(self):
        params = {'source_type': [ProjectSourceTypeChoices.DATA_SOURCE]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_activation_policy(self):
        params = {'activation_policy': [ActivationPolicyChoices.AUTOMATIC_IF_VALID]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 2)

    def test_enabled(self):
        params = {'enabled': True}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 4)

    def test_data_source(self):
        params = {'data_source_id': [self.data_sources[0].pk]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)
        params = {'data_source': [self.data_sources[1].name]}
        self.assertEqual(self.filterset(params, self.queryset).qs.count(), 1)

    def test_storage_key_not_filterable(self):
        self.assertNotIn('storage_key', self.filterset.get_filters())

    def test_active_revision_not_filterable(self):
        filters = self.filterset.get_filters()
        self.assertNotIn('active_revision', filters)
        self.assertNotIn('active_revision_id', filters)
