from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import post_save
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse

from core.models import ObjectChange, ObjectType
from core.tables import ObjectChangeTable
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject
from netbox_scripts.tests.plugin_testing import PluginTestCases
from users.models import ObjectPermission
from utilities.testing import TestCase, create_tags, post_data


class NetBoxScriptViewSetTestCase(PluginTestCases.DerivedObjectViewTestCase):
    model = NetBoxScript

    @classmethod
    def setUpTestData(cls):
        project = ScriptProject.objects.create(name='Surface Project', key='surface-project')
        scripts = (
            NetBoxScript(project=project, module_path='a', class_name='First', display_name='First'),
            NetBoxScript(project=project, module_path='b', class_name='Second', display_name='Second'),
            NetBoxScript(project=project, module_path='c', class_name='Third', display_name='Third'),
        )
        for script in scripts:
            script.save()

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        # Only the administrator's fields. Everything else is editable=False, so a posted
        # value would be ignored and assertInstanceEqual would fail on it.
        cls.form_data = {
            'enabled': False,
            'comments': 'Paused',
            'tags': [t.pk for t in tags],
        }
        # enabled leads: the first key drives the constrained-permission test and must
        # transition, and all three fixtures default to enabled.
        cls.bulk_edit_data = {
            'enabled': False,
        }

    def test_a_bulk_edit_does_not_revert_a_published_field(self):
        # Bulk edit materializes the whole selection before its loop and carries no stale-form
        # check, so a publication landing mid-loop is written back from the cached copy. The
        # receiver is that landing, timed against the first row the loop saves.
        scripts = list(NetBoxScript.objects.filter(class_name__in=('First', 'Second')))
        published = {}

        def publish_between(sender, instance, **kwargs):
            if published:
                return
            other = next(script for script in scripts if script.pk != instance.pk)
            NetBoxScript.objects.filter(pk=other.pk).update(display_name='Renamed By Sync')
            published['pk'] = other.pk

        post_save.connect(publish_between, sender=NetBoxScript)
        self.addCleanup(post_save.disconnect, publish_between, sender=NetBoxScript)
        obj_perm = ObjectPermission(name='Bulk edit scripts', actions=['view', 'change'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NetBoxScript))

        response = self.client.post(
            self._get_url('bulk_edit'),
            data={
                'pk': [script.pk for script in scripts],
                '_apply': True,
                'changelog_message': 'Pausing',
                **post_data({'enabled': False}),
            },
        )

        # A rejected form re-renders at 200 and saves nothing, which would pass every assertion below.
        self.assertHttpStatus(response, 302)
        clobbered = NetBoxScript.objects.get(pk=published['pk'])
        self.assertEqual(clobbered.display_name, 'Renamed By Sync')
        self.assertFalse(clobbered.enabled)


class NetBoxScriptViewTestCase(TestCase):
    """The detail view and the change-log rendering that reverses a Script's URL."""

    user_permissions = ('netbox_scripts.view_netboxscript',)

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='View Script Project', key='view-script-project')
        cls.script = NetBoxScript.objects.create(
            project=cls.project,
            module_path='tools.deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            metadata={'commit_default': True},
        )

    def test_get_absolute_url_resolves(self):
        # The inherited implementation reverses with no fallback, so it needs the detail route.
        self.assertEqual(self.script.get_absolute_url(), f'/plugins/netbox-scripts/scripts/{self.script.pk}/')

    def test_the_detail_view_renders(self):
        response = self.client.get(self.script.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn('tools.deploy', content)
        self.assertIn('DeployDevices', content)

    def test_the_detail_view_fetches_its_relations_in_the_object_query(self):
        # Both panels render a related object, so without the join each is its own query.
        self.client.get(self.script.get_absolute_url())  # warm the session and permission caches

        with CaptureQueriesContext(connection) as captured:
            self.client.get(self.script.get_absolute_url())

        tables = ('netbox_scripts_scriptproject', 'netbox_scripts_scriptprojectrevision')
        followups = [
            entry['sql']
            for entry in captured.captured_queries
            if any(table in entry['sql'] for table in tables) and 'netbox_scripts_netboxscript' not in entry['sql']
        ]
        self.assertEqual(followups, [])

    def test_the_identifier_is_the_one_every_detail_page_shows(self):
        # generic/object.html centres that row and exposes no block reaching its alignment,
        # so anything added here shifts either the reference or the breadcrumb.
        content = self.client.get(self.script.get_absolute_url()).content.decode()

        identifier = ' '.join(content[content.find('<code class="d-block text-muted') :][:400].split())
        self.assertIn(f'netbox_scripts.netboxscript:{self.script.pk}', identifier)
        self.assertNotIn('tools.deploy.DeployDevices', identifier)

    def test_the_breadcrumbs_reverse_the_list_route(self):
        # The default breadcrumb block reverses the model's list route, which the template
        # used to have to replace because no such route existed.
        response = self.client.get(self.script.get_absolute_url())
        self.assertIn('/plugins/netbox-scripts/scripts/', response.content.decode())

    def test_the_list_view_renders(self):
        response = self.client.get('/plugins/netbox-scripts/scripts/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('Deploy Devices', response.content.decode())

    def test_the_list_view_requires_permission(self):
        client = Client()
        client.force_login(get_user_model().objects.create_user(username='listless'))
        response = client.get('/plugins/netbox-scripts/scripts/')
        self.assertEqual(response.status_code, 403)

    def test_no_create_route_is_registered(self):
        # Rows are derived from an activated revision, so authoring one has no route.
        for name in ('netboxscript_add', 'netboxscript_bulk_delete'):
            with self.subTest(route=name), self.assertRaises(NoReverseMatch):
                reverse(f'plugins:netbox_scripts:{name}')

    def test_no_delete_route_is_registered(self):
        # Retirement replaces deletion so accumulated Job history survives.
        with self.assertRaises(NoReverseMatch):
            reverse('plugins:netbox_scripts:netboxscript_delete', args=[self.script.pk])

    def test_the_edit_route_requires_the_change_permission(self):
        url = reverse('plugins:netbox_scripts:netboxscript_edit', args=[self.script.pk])
        self.assertEqual(self.client.get(url).status_code, 403)

        self.add_permissions('netbox_scripts.change_netboxscript')
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_the_inherited_feature_tabs_render(self):
        # JobsMixin and the PrimaryModel feature set register these routes automatically, so
        # they are reachable whether or not this task set them up, and they render the same
        # per-model template.
        self.add_permissions(
            'netbox_scripts.view_netboxscript',
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


class ScriptFileTestCase(PluginTestCases.NestedObjectViewTestCase):
    model = ScriptFile

    @classmethod
    def setUpTestData(cls):
        projects = (
            ScriptProject(name='View Project 1', key='view-project-1'),
            ScriptProject(name='View Project 2', key='view-project-2'),
        )
        for project in projects:
            project.save()

        objs = (
            ScriptFile(project=projects[0], source_path='tools/first.py', description='First'),
            ScriptFile(project=projects[0], source_path='tools/second.py', description='Second'),
            ScriptFile(
                project=projects[1],
                source_path='tools/third.py',
                description='Third',
                enabled=False,
            ),
        )
        for obj in objs:
            obj.save()

        tags = create_tags('Alpha', 'Bravo', 'Charlie')

        # Identity fields are here for the 403 path only. The edit assertions strip them.
        cls.form_data = {
            'project': projects[0].pk,
            'source_path': 'tools/created.py',
            'description': 'Form-created script file',
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

    def test_no_create_or_delete_route_is_registered(self):
        # Per route, because bulk_delete takes no pk while delete does.
        routes = (
            ('plugins:netbox_scripts:scriptfile_add', {}),
            ('plugins:netbox_scripts:scriptfile_bulk_delete', {}),
            ('plugins:netbox_scripts:scriptfile_delete', {'pk': 1}),
        )
        for name, kwargs in routes:
            with self.subTest(route=name), self.assertRaises(NoReverseMatch):
                reverse(name, kwargs=kwargs)

    def test_the_detail_page_offers_no_delete_or_clone_action(self):
        """ObjectView.actions defaults to Clone, Edit and Delete, filtered by permission not route."""
        self.add_permissions(
            'netbox_scripts.view_scriptfile',
            'netbox_scripts.add_scriptfile',
            'netbox_scripts.change_scriptfile',
            'netbox_scripts.delete_scriptfile',
        )
        script_file = ScriptFile.objects.first()

        response = self.client.get(script_file.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual({str(action.label) for action in response.context['actions']}, {'Edit'})
