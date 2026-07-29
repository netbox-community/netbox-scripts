from django.urls import reverse

from core.models import DataSource, ObjectType
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.tests.plugin_testing import PluginTestCases
from users.models import ObjectPermission
from utilities.testing import TestCase, create_tags, create_test_user


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


class CustomScriptProjectEntrypointsViewTestCase(TestCase):
    """The Entrypoints tab writes declarations, so it carries the Module permission."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Tab Project', key='tab-project')
        CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            manifest=[{'path': path, 'size': 1, 'sha256': 'a' * 64} for path in ('deploy.py', 'tools/audit.py')],
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def url(self):
        return reverse('plugins:netbox_custom_scripts:customscriptproject_entrypoints', args=[self.project.pk])

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def grant_both(self):
        # The tab restricts the project queryset and writes declarations, so it needs both.
        self.grant(CustomScriptProject, 'view', 'change')
        self.grant(CustomScriptModule, 'view', 'change', 'add')

    def test_the_tab_lists_the_candidates(self):
        self.grant_both()
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        self.assertIn('tools/audit.py', response.content.decode())

    def test_selecting_creates_declarations(self):
        self.grant_both()
        response = self.client.post(self.url(), {'entrypoints': ['deploy.py', 'tools/audit.py']})
        self.assertHttpStatus(response, 302)
        self.assertEqual(
            sorted(self.project.modules.filter(enabled=True).values_list('source_path', flat=True)),
            ['deploy.py', 'tools/audit.py'],
        )

    def test_deselecting_disables_and_keeps_the_row(self):
        self.grant_both()
        module = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        self.assertHttpStatus(self.client.post(self.url(), {'entrypoints': []}), 302)
        module.refresh_from_db()
        self.assertFalse(module.enabled)

    def test_the_project_permission_alone_is_not_enough(self):
        # Writing declarations needs their own permission, not just the project's.
        self.grant(CustomScriptProject, 'view', 'change')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_module_permission_alone_is_not_enough(self):
        self.grant(CustomScriptModule, 'view', 'change', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_a_project_without_source_renders_an_empty_selection(self):
        self.grant_both()
        bare = CustomScriptProject.objects.create(name='Bare Project', key='bare-project')
        url = reverse('plugins:netbox_custom_scripts:customscriptproject_entrypoints', args=[bare.pk])
        self.assertHttpStatus(self.client.get(url), 200)
