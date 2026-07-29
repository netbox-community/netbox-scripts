from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject
from netbox_custom_scripts.tests.plugin_testing import PluginTestCases
from utilities.testing import create_tags


class CustomScriptModuleTestCase(PluginTestCases.PrimaryObjectViewTestCase):
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

        cls.csv_data = (
            'project,source_path,enabled,description',
            'view-project-1,tools/fourth.py,true,Bulk-imported',
            'view-project-1,tools/fifth.py,true,Bulk-imported',
            'view-project-2,tools/sixth.py,false,Bulk-imported',
        )

        cls.csv_update_data = (
            'id,description,comments',
            f'{objs[0].pk},First updated,Note 1',
            f'{objs[1].pk},Second updated,Note 2',
            f'{objs[2].pk},Third updated,Note 3',
        )

        cls.bulk_edit_data = {
            'description': 'Bulk-edited description',
            'enabled': False,
            'comments': 'Bulk-edited notes',
        }
