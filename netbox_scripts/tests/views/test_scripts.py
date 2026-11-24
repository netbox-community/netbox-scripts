import csv
import io
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import post_save, pre_save
from django.http import QueryDict
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import NoReverseMatch, reverse
from django.utils.html import escape

from core.models import ObjectChange, ObjectType
from core.tables import ObjectChangeTable
from extras.models import TableConfig
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject
from netbox_scripts.permissions import SELECTION_REFUSAL
from netbox_scripts.preferences import SCRIPT_LIST_VIEW
from netbox_scripts.tables import NetBoxScriptGroupedTable, NetBoxScriptTable
from netbox_scripts.tests.plugin_testing import (
    ObjectPermissionTestMixin,
    PluginTestCases,
    create_scripts_out_of_default_order,
    refresh_count,
)
from users.models import ObjectPermission, UserConfig
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

    def test_a_bulk_edit_saves_in_primary_key_order(self):
        pks = create_scripts_out_of_default_order()
        saved = []

        def record_save(sender, instance, **kwargs):
            saved.append(instance.pk)

        pre_save.connect(record_save, sender=NetBoxScript, dispatch_uid='bulk-edit-save-order')
        self.addCleanup(pre_save.disconnect, record_save, sender=NetBoxScript, dispatch_uid='bulk-edit-save-order')
        self.add_permissions('netbox_scripts.view_netboxscript', 'netbox_scripts.change_netboxscript')

        response = self.client.post(
            self._get_url('bulk_edit'),
            data={'pk': pks[::-1], '_apply': True, **post_data({'enabled': False})},
        )

        self.assertHttpStatus(response, 302)
        self.assertEqual(saved, pks)

    def test_the_bulk_edit_confirmation_preserves_default_order(self):
        pks = create_scripts_out_of_default_order()
        self.add_permissions('netbox_scripts.view_netboxscript', 'netbox_scripts.change_netboxscript')

        response = self.client.post(self._get_url('bulk_edit'), data={'pk': pks})

        self.assertHttpStatus(response, 200)
        self.assertEqual([row.record.pk for row in response.context['table'].rows], [pks[1], pks[2], pks[0]])


class NetBoxScriptViewTestCase(TestCase):
    """The list and detail views, and the change-log rendering that reverses a Script's URL."""

    user_permissions = ('netbox_scripts.view_netboxscript',)
    grouped_outline = [
        ('audit-project', ['tools.deploy.AuditPrefixes']),
        ('view-script-project', ['tools.deploy.DeployDevices', 'tools.deploy.Rollback', 'tools.naming.RenameSites']),
    ]

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
        NetBoxScript.objects.create(
            project=cls.project, module_path='tools.deploy', class_name='Rollback', display_name='Rollback'
        )
        NetBoxScript.objects.create(
            project=cls.project, module_path='tools.naming', class_name='RenameSites', display_name='Rename Sites'
        )
        cls.audit_project = ScriptProject.objects.create(name='Audit Project', key='audit-project')
        NetBoxScript.objects.create(
            project=cls.audit_project,
            module_path='tools.deploy',
            class_name='AuditPrefixes',
            display_name='Audit Prefixes',
        )

    def list_page(self, **params):
        return self.client.get(reverse('plugins:netbox_scripts:netboxscript_list'), params)

    def outline(self, response):
        """Return the grouped page's Project keys and Script full names, in page order."""
        return [
            (project.key, [row.record.full_name for row in rows]) for project, rows in response.context['table'].groups
        ]

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

    def test_the_grouped_view_lists_scripts_under_project_headings(self):
        self.add_permissions('netbox_scripts.view_scriptproject')

        response = self.list_page(view='grouped')
        body = response.content.decode()

        self.assertHttpStatus(response, 200)
        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)
        self.assertEqual(self.outline(response), self.grouped_outline)
        for project in (self.project, self.audit_project):
            self.assertIn(project.get_absolute_url(), body)
        for script in NetBoxScript.objects.all():
            # The Changelog link starts with the Script's URL, so only the name link remains once it is removed.
            changelog_url = reverse('plugins:netbox_scripts:netboxscript_changelog', kwargs={'pk': script.pk})
            self.assertIn(script.get_absolute_url(), body.replace(changelog_url, ''))
        position = 0
        for text in (
            'Audit Project',
            'Audit Prefixes',
            'tools.deploy.AuditPrefixes',
            'View Script Project',
            'Deploy Devices',
            'tools.deploy.DeployDevices',
            'Rollback',
            'tools.deploy.Rollback',
            'Rename Sites',
            'tools.naming.RenameSites',
        ):
            position = body.find(text, position)
            self.assertNotEqual(position, -1, text)
            position += len(text)

    def test_projects_sharing_a_name_keep_separate_groups(self):
        lab_one = ScriptProject.objects.create(name='Lab', key='lab-one')
        lab_two = ScriptProject.objects.create(name='Lab', key='lab-two')
        for project, module_path, class_name in (
            (lab_one, 'a', 'Alpha'),
            (lab_one, 'c', 'Gamma'),
            (lab_two, 'b', 'Beta'),
        ):
            NetBoxScript.objects.create(
                project=project, module_path=module_path, class_name=class_name, display_name=class_name
            )

        response = self.list_page(view='grouped', project_id=[lab_one.pk, lab_two.pk])

        self.assertEqual(
            self.outline(response),
            [('lab-one', ['a.Alpha', 'c.Gamma']), ('lab-two', ['b.Beta'])],
        )

    def test_a_sort_does_not_reorder_the_grouped_view(self):
        response = self.list_page(view='grouped', sort='-class_name')

        self.assertEqual(self.outline(response), self.grouped_outline)

    def test_the_grouped_view_applies_search_and_filters(self):
        response = self.list_page(view='grouped', project_id=self.audit_project.pk, q='Audit')

        self.assertEqual(self.outline(response), self.grouped_outline[:1])

    def test_the_grouped_view_lists_only_viewable_scripts(self):
        user = get_user_model().objects.create_user(username='audit-viewer')
        permission = ObjectPermission.objects.create(
            name='Audit Scripts', actions=['view'], constraints={'project__key': 'audit-project'}
        )
        permission.users.add(user)
        permission.object_types.add(ObjectType.objects.get_for_model(NetBoxScript))
        client = Client()
        client.force_login(user)

        response = client.get(reverse('plugins:netbox_scripts:netboxscript_list'), {'view': 'grouped'})

        self.assertEqual(self.outline(response), self.grouped_outline[:1])
        self.assertNotIn('View Script Project', response.content.decode())

    def test_a_project_continuing_on_the_next_page_repeats_its_heading(self):
        # Project view covers the off-page Audit Project too, so the lookup must stay on the page.
        self.add_permissions('netbox_scripts.view_scriptproject')
        # NetBox's paginator folds up to five trailing rows into the previous page, so page 2 needs eight rows.
        for class_name in ('Renumber', 'Reorder', 'Replace', 'Reroute'):
            NetBoxScript.objects.create(
                project=self.project, module_path='tools.naming', class_name=class_name, display_name=class_name
            )

        response = self.list_page(view='grouped', per_page=2, page=2)

        self.assertEqual(
            self.outline(response),
            [
                (
                    'view-script-project',
                    [
                        'tools.deploy.Rollback',
                        'tools.naming.RenameSites',
                        'tools.naming.Renumber',
                        'tools.naming.Reorder',
                        'tools.naming.Replace',
                        'tools.naming.Reroute',
                    ],
                )
            ],
        )
        self.assertEqual(response.context['table'].viewable_project_ids, {self.project.pk})

    def test_the_chosen_view_is_saved_for_the_user(self):
        self.list_page(view='grouped')
        self.assertEqual(UserConfig.objects.get(user=self.user).get(SCRIPT_LIST_VIEW), 'grouped')

        # The Filters form and "Clear all" both drop the view parameter.
        response = self.list_page(project_id=self.audit_project.pk)
        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)
        response = self.list_page()
        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)

        form = self.client.get(reverse('account:preferences')).context['form']
        self.assertIn(SCRIPT_LIST_VIEW, form.fields)
        self.assertEqual(form[SCRIPT_LIST_VIEW].value(), 'grouped')

        response = self.list_page(view='table')
        self.assertEqual(UserConfig.objects.get(user=self.user).get(SCRIPT_LIST_VIEW), 'table')
        self.assertIs(type(response.context['table']), NetBoxScriptTable)

    def test_an_unknown_view_is_ignored_and_not_saved(self):
        response = self.list_page(view='bogus')
        self.assertIs(type(response.context['table']), NetBoxScriptTable)
        self.assertIsNone(UserConfig.objects.get(user=self.user).get(SCRIPT_LIST_VIEW))

        self.list_page(view='grouped')
        response = self.list_page(view='bogus')
        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)
        self.assertEqual(UserConfig.objects.get(user=self.user).get(SCRIPT_LIST_VIEW), 'grouped')

    def test_a_view_switch_in_maintenance_mode_is_not_saved(self):
        self.user.config.set(SCRIPT_LIST_VIEW, 'table', commit=True)

        maintenance = SimpleNamespace(MAINTENANCE_MODE=True)
        with mock.patch('netbox_scripts.views.scripts.get_config', return_value=maintenance):
            response = self.list_page(view='grouped')

        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)
        self.assertEqual(UserConfig.objects.get(user=self.user).get(SCRIPT_LIST_VIEW), 'table')

    @override_settings(LOGIN_REQUIRED=False, EXEMPT_VIEW_PERMISSIONS=['*'])
    def test_an_anonymous_viewer_can_switch_views(self):
        client = Client()
        url = reverse('plugins:netbox_scripts:netboxscript_list')

        response = client.get(url, {'view': 'grouped'})
        self.assertHttpStatus(response, 200)
        self.assertIs(type(response.context['table']), NetBoxScriptGroupedTable)
        # Exempt view permissions cover Projects too, so both cards offer View Project.
        for project in (self.project, self.audit_project):
            self.assertIn(project.get_absolute_url(), response.content.decode())

        response = client.get(url)
        self.assertHttpStatus(response, 200)
        self.assertIs(type(response.context['table']), NetBoxScriptTable)

    def test_an_export_keeps_the_table_columns(self):
        self.user.config.set('tables.NetBoxScriptTable.columns', ['display_name', 'description'])
        self.user.config.set(SCRIPT_LIST_VIEW, 'grouped', commit=True)

        response = self.list_page(export='table')

        table = NetBoxScriptTable(NetBoxScript.objects.none())
        self.assertEqual(
            next(csv.reader(io.StringIO(response.content.decode()))),
            [str(table.columns[name].header) for name in ('display_name', 'description')],
        )

    def test_a_partial_update_keeps_the_table(self):
        self.user.config.set(SCRIPT_LIST_VIEW, 'grouped', commit=True)

        response = self.client.get(reverse('plugins:netbox_scripts:netboxscript_list'), headers={'hx-request': 'true'})

        self.assertTemplateUsed(response, 'htmx/table.html')
        self.assertTemplateNotUsed(response, 'netbox_scripts/netboxscript_list.html')
        self.assertIs(type(response.context['table']), NetBoxScriptTable)

    def test_the_view_switch_keeps_filters_and_drops_page_sort_and_table_config(self):
        projects = [self.project.pk, self.audit_project.pk]
        config = TableConfig.objects.create(
            name='Script names',
            object_type=ObjectType.objects.get_for_model(NetBoxScript),
            table='NetBoxScriptTable',
            columns=['display_name', 'project'],
        )

        response = self.list_page(
            project_id=projects, q='dep', per_page=25, page=2, sort='class_name', tableconfig_id=config.pk
        )

        body = response.content.decode()
        for name, view in (('table_view_url', 'table'), ('grouped_view_url', 'grouped')):
            with self.subTest(url=name):
                url = response.context[name]
                params = QueryDict(url[1:])
                self.assertEqual(params.getlist('project_id'), [str(pk) for pk in projects])
                self.assertEqual((params['q'], params['per_page'], params['view']), ('dep', '25', view))
                self.assertNotIn('page', params)
                self.assertNotIn('sort', params)
                self.assertNotIn('tableconfig_id', params)
                self.assertIn(escape(url), body)

    def test_the_grouped_view_adds_no_query_per_script(self):
        # With Project view, every grouped page also runs the View Project lookup.
        self.add_permissions('netbox_scripts.view_scriptproject')
        self.list_page(view='grouped')
        with CaptureQueriesContext(connection) as before:
            self.list_page(view='grouped')
        for key in ('extra-one', 'extra-two'):
            project = ScriptProject.objects.create(name=key, key=key)
            NetBoxScript.objects.create(project=project, module_path='extra', class_name='Extra', display_name=key)

        with CaptureQueriesContext(connection) as after:
            self.list_page(view='grouped')

        self.assertEqual(len(after), len(before))

    def test_view_project_is_offered_only_for_viewable_projects(self):
        body = self.list_page(view='grouped').content.decode()
        for project in (self.project, self.audit_project):
            self.assertIn(project.name, body)
            self.assertNotIn(project.get_absolute_url(), body)

        permission = ObjectPermission.objects.create(
            name='Audit Project', actions=['view'], constraints={'key': 'audit-project'}
        )
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(ScriptProject))

        body = self.list_page(view='grouped').content.decode()
        self.assertIn(self.audit_project.get_absolute_url(), body)
        self.assertNotIn(self.project.get_absolute_url(), body)

    def test_both_views_offer_the_tables_row_actions(self):
        edit_url = reverse('plugins:netbox_scripts:netboxscript_edit', kwargs={'pk': self.script.pk})
        changelog_url = reverse('plugins:netbox_scripts:netboxscript_changelog', kwargs={'pk': self.script.pk})
        for view in ('table', 'grouped'):
            with self.subTest(view=view):
                body = self.list_page(view=view).content.decode()
                self.assertIn(changelog_url, body)
                self.assertNotIn(edit_url, body)

        self.add_permissions('netbox_scripts.change_netboxscript')
        for view in ('table', 'grouped'):
            with self.subTest(view=view, change=True):
                self.assertIn(edit_url, self.list_page(view=view).content.decode())


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


# Without view permission, the edit form's disabled Project field fails validation.
@override_settings(EXEMPT_VIEW_PERMISSIONS=['*'], EXEMPT_EXCLUDE_MODELS=[])
class ScriptFileSelectionViewTestCase(ObjectPermissionTestMixin, TestCase):
    """Enabling or disabling a Script File on its edit page follows its Project's Script Files tab."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Selection Project', key='selection-project')
        cls.other = ScriptProject.objects.create(name='Selection Elsewhere', key='selection-elsewhere')
        cls.first = ScriptFile.objects.create(project=cls.project, source_path='first.py')
        cls.elsewhere = ScriptFile.objects.create(project=cls.other, source_path='elsewhere.py', enabled=False)

    def edit(self, script_file, **data):
        url = reverse('plugins:netbox_scripts:scriptfile_edit', args=[script_file.pk])
        return self.client.post(url, post_data(data))

    def test_enabling_or_disabling_restages_the_project(self):
        self.add_permissions('netbox_scripts.change_scriptfile', 'netbox_scripts.change_scriptproject')
        for script_file, enabled in ((self.first, False), (self.elsewhere, True)):
            with self.subTest(source_path=script_file.source_path):
                self.assertHttpStatus(self.edit(script_file, enabled=enabled), 302)
                script_file.refresh_from_db()
                self.assertEqual(script_file.enabled, enabled)
                self.assertEqual(refresh_count(script_file.project), 1)

    def test_disabling_needs_the_project_change_permission(self):
        self.add_permissions('netbox_scripts.change_scriptfile')

        response = self.edit(self.first, enabled=False)

        self.assertHttpStatus(response, 200)
        self.assertIn(str(SELECTION_REFUSAL), response.context['form'].non_field_errors())
        self.first.refresh_from_db()
        self.assertTrue(self.first.enabled)
        self.assertEqual(refresh_count(self.project), 0)

    def test_an_edit_that_keeps_the_selection_needs_only_the_script_file_permission(self):
        self.add_permissions('netbox_scripts.change_scriptfile')

        response = self.edit(self.first, enabled=True, description='Edited')

        self.assertHttpStatus(response, 302)
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, 'Edited')
        self.assertEqual(refresh_count(self.project), 0)

    def test_a_project_permission_scoped_to_another_project_does_not_count(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        self.grant(ScriptProject, 'change', constraints={'pk': self.other.pk})

        self.assertHttpStatus(self.edit(self.first, enabled=False), 200)
        self.first.refresh_from_db()
        self.assertTrue(self.first.enabled)
