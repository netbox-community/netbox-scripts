from core.models import DataSource
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_custom_scripts.models import CustomScriptProject
from netbox_custom_scripts.tests.plugin_testing import PluginTestCases
from utilities.testing import create_tags


class CustomScriptProjectTestCase(PluginTestCases.PrimaryObjectViewTestCase):
    model = CustomScriptProject

    @classmethod
    def setUpTestData(cls):
        data_source = DataSource.objects.create(
            name='Data Source 1',
            type='local',
            source_url='file:///tmp/data-source-1/',
        )

        objs = (
            CustomScriptProject(name='CustomScriptProject 1', key='project-1', description='First'),
            CustomScriptProject(name='CustomScriptProject 2', key='project-2', description='Second'),
            CustomScriptProject(
                name='CustomScriptProject 3',
                key='project-3',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=data_source,
                data_path='automation/netbox',
                enabled=False,
            ),
        )
        for obj in objs:
            obj.save()

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        cls.form_data = {
            'name': 'CustomScriptProject X',
            'key': 'project-x',
            'description': 'Form-created project',
            'source_type': ProjectSourceTypeChoices.UPLOAD,
            'data_path': '',
            'activation_policy': ActivationPolicyChoices.MANUAL,
            'enabled': True,
            'comments': 'Some notes',
            'tags': [t.pk for t in tags],
        }

        cls.csv_data = (
            'name,key,source_type,activation_policy,enabled,description,comments',
            'CustomScriptProject 4,project-4,upload,manual,true,Bulk-imported,',
            'CustomScriptProject 5,project-5,upload,manual,true,Bulk-imported,',
            'CustomScriptProject 6,project-6,upload,automatic_if_valid,false,Bulk-imported,',
        )

        cls.csv_update_data = (
            'id,name,description,comments',
            f'{objs[0].pk},CustomScriptProject 1 Updated,Updated first,Note 1',
            f'{objs[1].pk},CustomScriptProject 2 Updated,Updated second,Note 2',
            f'{objs[2].pk},CustomScriptProject 3 Updated,Updated third,Note 3',
        )

        cls.bulk_edit_data = {
            'description': 'Bulk-edited description',
            'enabled': False,
            'comments': 'Bulk-edited notes',
        }

    def _form_data_without_identity_fields(self):
        # key and source_type are disabled on the edit form (immutable after
        # creation), so posted values are ignored and must not be asserted.
        return {key: value for key, value in self.form_data.items() if key not in ('key', 'source_type')}

    def test_edit_object_with_permission(self):
        self.form_data = self._form_data_without_identity_fields()
        super().test_edit_object_with_permission()

    def test_edit_object_with_constrained_permission(self):
        self.form_data = self._form_data_without_identity_fields()
        super().test_edit_object_with_constrained_permission()
