from django.contrib.auth import get_user_model
from django.test import Client

from core.models import ObjectChange
from core.tables import ObjectChangeTable
from netbox_custom_scripts.models import CustomScript, CustomScriptProject
from utilities.testing import TestCase


class CustomScriptViewTestCase(TestCase):
    """The detail view and the change-log rendering that reverses a Custom Script's URL."""

    user_permissions = ('netbox_custom_scripts.view_customscript',)

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='View Script Project', key='view-script-project')
        cls.script = CustomScript.objects.create(
            project=cls.project,
            module_path='tools.deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            metadata={'commit_default': True},
        )

    def test_get_absolute_url_resolves(self):
        # The inherited implementation reverses with no fallback, so it needs the detail route.
        self.assertEqual(self.script.get_absolute_url(), f'/plugins/custom-scripts/scripts/{self.script.pk}/')

    def test_the_detail_view_renders(self):
        response = self.client.get(self.script.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('tools.deploy', content)
        self.assertIn('DeployDevices', content)

    def test_the_breadcrumbs_lead_through_the_project(self):
        # The default breadcrumb block reverses the model's list route unconditionally, and
        # this model has none, so the template replaces the block rather than extending it.
        response = self.client.get(self.script.get_absolute_url())
        content = response.content.decode()
        self.assertIn(self.project.get_absolute_url(), content)
        self.assertIn('/plugins/custom-scripts/projects/', content)

    def test_the_inherited_feature_tabs_render(self):
        # JobsMixin and the PrimaryModel feature set register these routes automatically, so
        # they are reachable whether or not this task set them up, and they render the same
        # per-model template.
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'extras.view_journalentry',
            'core.view_objectchange',
        )
        for tab in ('changelog', 'journal', 'jobs'):
            with self.subTest(tab=tab):
                response = self.client.get(f'{self.script.get_absolute_url()}{tab}/')
                self.assertEqual(response.status_code, 200)

    def test_the_detail_view_requires_permission(self):
        client = Client()
        client.force_login(get_user_model().objects.create_user(username='unprivileged'))
        response = client.get(self.script.get_absolute_url())
        self.assertEqual(response.status_code, 403)

    def test_a_change_log_row_renders_the_object_link(self):
        # The object_repr column evaluates value.get_absolute_url, which is the call site that
        # raises NoReverseMatch when a change-logged model has no registered detail route. The
        # cell is rendered on its own rather than the whole table, so the assertion covers the
        # reverse without depending on the surrounding table template.
        change = self.script.to_objectchange('update')
        change.user = self.user
        change.request_id = '00000000-0000-0000-0000-000000000000'
        change.save()

        table = ObjectChangeTable(ObjectChange.objects.all())
        cell = next(iter(table.rows)).get_cell('object_repr')
        self.assertIn(self.script.get_absolute_url(), cell)
        self.assertIn('Deploy Devices', cell)
