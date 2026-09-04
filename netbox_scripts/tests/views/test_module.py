from netbox_scripts.models import CustomScriptModule, CustomScriptProject
from netbox_scripts.tests.plugin_testing import PluginTestCases
from utilities.testing import create_tags


class CustomScriptModuleTestCase(PluginTestCases.NestedObjectViewTestCase):
    model = CustomScriptModule

    @classmethod
    def setUpTestData(cls):
        projects = (
            CustomScriptProject(name='View Project 1', key='view-project-1'),
            CustomScriptProject(name='View Project 2', key='view-project-2'),
        )
        for project in projects:
            project.save()

        objs = (
            CustomScriptModule(project=projects[0], source_path='tools/first.py', description='First'),
            CustomScriptModule(project=projects[0], source_path='tools/second.py', description='Second'),
            CustomScriptModule(
                project=projects[1],
                source_path='tools/third.py',
                description='Third',
                enabled=False,
            ),
        )
        for obj in objs:
            obj.save()

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        # Posted canonical: save() canonicalizes, and posted values are compared to the saved row.
        cls.form_data = {
            'project': projects[0].pk,
            'source_path': 'tools/created.py',
            'description': 'Form-created module',
            'enabled': True,
            'comments': 'Some notes',
            'tags': [t.pk for t in tags],
        }

    def _form_data_without_identity_fields(self):
        # project and source_path are disabled on the edit form (frozen after creation), so
        # posted values are ignored and must not be asserted.
        return {key: value for key, value in self.form_data.items() if key not in ('project', 'source_path')}

    def test_edit_object_with_permission(self):
        self.form_data = self._form_data_without_identity_fields()
        super().test_edit_object_with_permission()

    def test_edit_object_with_constrained_permission(self):
        self.form_data = self._form_data_without_identity_fields()
        super().test_edit_object_with_constrained_permission()
