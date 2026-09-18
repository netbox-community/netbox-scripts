import uuid
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, override_settings
from django.urls import reverse

from core.models import DataSource, ObjectChange, ObjectType
from netbox.context_managers import event_tracking
from netbox_scripts import activation
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.models import (
    NetBoxScript,
    ScriptFile,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.storage import service
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin, PluginTestCases
from netbox_scripts.ui import ScriptProjectPanel, ScriptProjectStatePanel
from users.models import ObjectPermission
from utilities.testing import TestCase, create_tags, create_test_user

ACTIVATE_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


class ScriptProjectTestCase(PluginTestCases.PrimaryObjectViewTestCase):
    model = ScriptProject

    @classmethod
    def setUpTestData(cls):
        data_source = DataSource.objects.create(
            name='Data Source 1',
            type='local',
            source_url='file:///tmp/data-source-1/',
        )

        objs = (
            ScriptProject(name='ScriptProject 1', key='project-1', description='First'),
            ScriptProject(name='ScriptProject 2', key='project-2', description='Second'),
            ScriptProject(
                name='ScriptProject 3',
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
            'name': 'ScriptProject X',
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
            'ScriptProject 4,project-4,upload,manual,true,Bulk-imported,',
            'ScriptProject 5,project-5,upload,manual,true,Bulk-imported,',
            'ScriptProject 6,project-6,upload,automatic_if_valid,false,Bulk-imported,',
        )

        cls.csv_update_data = (
            'id,name,description,comments',
            f'{objs[0].pk},ScriptProject 1 Updated,Updated first,Note 1',
            f'{objs[1].pk},ScriptProject 2 Updated,Updated second,Note 2',
            f'{objs[2].pk},ScriptProject 3 Updated,Updated third,Note 3',
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


class ScriptProjectScriptFilesViewTestCase(ObjectPermissionTestMixin, TestCase):
    """The Script Files tab writes declarations, so it carries the Script File permission."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Tab Project', key='tab-project')
        ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            manifest=[{'path': path, 'size': 1, 'sha256': 'a' * 64} for path in ('deploy.py', 'tools/audit.py')],
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def url(self):
        return reverse('plugins:netbox_scripts:scriptproject_script_files', args=[self.project.pk])

    def grant_both(self):
        # The tab restricts the project queryset and writes declarations, so it needs both.
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'view', 'change', 'add')

    def test_the_tab_lists_the_candidates(self):
        self.grant_both()
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        self.assertIn('tools/audit.py', response.content.decode())

    def test_selecting_creates_declarations(self):
        self.grant_both()
        response = self.client.post(self.url(), {'script_files_1': ['deploy.py', 'tools/audit.py']})
        self.assertHttpStatus(response, 302)
        self.assertEqual(
            sorted(self.project.script_files.filter(enabled=True).values_list('source_path', flat=True)),
            ['deploy.py', 'tools/audit.py'],
        )

    def test_deselecting_disables_and_keeps_the_row(self):
        self.grant_both()
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        self.assertHttpStatus(self.client.post(self.url(), {'script_files_1': []}), 302)
        script_file.refresh_from_db()
        self.assertFalse(script_file.enabled)

    def test_the_project_permission_alone_is_not_enough(self):
        # Writing declarations needs their own permission, not just the project's.
        self.grant(ScriptProject, 'view', 'change')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_script_file_permission_alone_is_not_enough(self):
        self.grant(ScriptFile, 'view', 'change', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def panes(self, body):
        """Return the available and the selected pane markup, split on the two subwidget ids."""
        available = body.split('id="id_script_files_0"', 1)[1].split('</select>', 1)[0]
        selected = body.split('id="id_script_files_1"', 1)[1].split('</select>', 1)[0]
        return available, selected

    def test_the_choices_reach_both_panes(self):
        # A MultiWidget carries no choices descriptor, so setting them on the field alone leaves
        # both panes empty and the tab unusable.
        self.grant_both()
        ScriptFile.objects.create(project=self.project, source_path='deploy.py', enabled=True)

        available, selected = self.panes(self.client.get(self.url()).content.decode())

        self.assertIn('tools/audit.py', available)
        self.assertNotIn('tools/audit.py', selected)
        self.assertIn('deploy.py', selected)

    def test_the_tab_offers_no_field_it_would_discard(self):
        # save() reconciles declarations and never saves the project, so an attribute field here
        # would take input and silently drop it.
        self.grant_both()
        body = self.client.get(self.url()).content.decode()
        for discarded in ('"id_owner"', '"id_owner_group"', '"id_comments"'):
            self.assertNotIn(discarded, body)

    def test_a_project_without_source_renders_an_empty_selection(self):
        self.grant_both()
        bare = ScriptProject.objects.create(name='Bare Project', key='bare-project')
        url = reverse('plugins:netbox_scripts:scriptproject_script_files', args=[bare.pk])
        self.assertHttpStatus(self.client.get(url), 200)

    def test_the_tab_says_what_saving_does_and_names_the_other_tab(self):
        # ViewTab carries no description, so the line rides on the field's help text, which
        # NetBox renders beneath the label.
        self.grant_both()
        body = self.client.get(self.url()).content.decode()
        # The whole sentence, because the tab bar renders the words "Revision Files" regardless.
        self.assertIn('Saving a change restages the source, and the Revision Files tab lists', body)

    def test_selection_respects_the_child_add_scope(self):
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'change')
        self.grant(ScriptFile, 'add', constraints={'project_id': self.project.pk + 1000})
        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.client.post(self.url(), {'script_files_1': ['deploy.py']})
        self.assertHttpStatus(response, 200)
        self.assertFalse(self.project.script_files.exists())
        flush.assert_not_called()

    def test_posting_a_path_that_cannot_be_declared_re_renders_the_form(self):
        self.grant_both()
        project = ScriptProject.objects.create(name='Hyphen Project', key='hyphen-project')
        ScriptProjectRevision.objects.create(
            project=project,
            digest='9' * 64,
            manifest=[{'path': 'my-file.py', 'size': 1, 'sha256': 'a' * 64}],
            status=RevisionStatusChoices.MATERIALIZED,
        )
        url = reverse('plugins:netbox_scripts:scriptproject_script_files', args=[project.pk])

        response = self.client.post(url, {'script_files_1': ['my-file.py']})

        self.assertHttpStatus(response, 200)
        # The attribution, not the page text: save()'s AbortRequest renders the same message as a toast.
        self.assertIn('not a valid Python identifier', response.context['form'].errors['script_files'][0])
        self.assertFalse(ScriptFile.objects.filter(project=project).exists())

    def test_posting_two_paths_with_one_module_name_re_renders_the_form(self):
        self.grant_both()
        project = ScriptProject.objects.create(name='Collision Project', key='collision-project')
        ScriptProjectRevision.objects.create(
            project=project,
            digest='8' * 64,
            manifest=[{'path': path, 'size': 1, 'sha256': 'a' * 64} for path in ('deploy.py', 'deploy/__init__.py')],
            status=RevisionStatusChoices.MATERIALIZED,
        )
        url = reverse('plugins:netbox_scripts:scriptproject_script_files', args=[project.pk])

        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.client.post(url, {'script_files_1': ['deploy.py', 'deploy/__init__.py']})

        self.assertHttpStatus(response, 200)
        self.assertIn('imports as', response.content.decode())
        # The view's atomic wraps form.save(), so the row written before the refusal rolls back.
        self.assertFalse(ScriptFile.objects.filter(project=project).exists())
        flush.assert_not_called()


class ScriptProjectSourceStateViewTestCase(ObjectPermissionTestMixin, TestCase):
    """The detail view surfaces source state, revision history, and the add-script action."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='State Project', key='state-project')
        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='d' * 64,
            status=RevisionStatusChoices.VALID,
            manifest=[{'path': 'deploy.py', 'size': 1, 'sha256': 'e' * 64}],
            file_count=1,
            total_size=1,
        )

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def body(self):
        response = self.client.get(self.project.get_absolute_url())
        self.assertHttpStatus(response, 200)
        return response.content.decode()

    def test_the_project_panel_reports_a_revision_awaiting_activation(self):
        self.grant(ScriptProject, 'view')
        self.assertIn('waiting to be activated', self.body())

    def test_the_state_line_is_not_inside_the_current_revision_panel(self):
        # The sentence describes the newest revision, the panel below it describes the served one.
        self.grant(ScriptProject, 'view')
        body = self.body()
        self.assertLess(body.index('waiting to be activated'), body.index('Current revision'))
        # And where the fact lives, so declaring it back on the revision panel fails here too.
        self.assertIn('source_state', ScriptProjectPanel._attrs)
        self.assertNotIn('source_state', ScriptProjectStatePanel._attrs)

    def test_the_panel_describes_the_current_revision(self):
        self.grant(ScriptProject, 'view')
        body = self.body()
        # Date, status, digest, files, size, activation, per the panel's contract.
        self.assertIn('d' * 12, body)
        self.assertIn('Valid', body)
        self.assertIn('Current revision', body)

    def test_the_panel_links_the_current_revision(self):
        self.grant(ScriptProject, 'view')
        self.assertIn(self.revision.get_absolute_url(), self.body())

    def test_the_panel_omits_revision_implementation_fields(self):
        # The manifest and the full digest belong to a diagnostic view. The project's own
        # storage key is a separate decision, and the Project panel has always shown it.
        self.grant(ScriptProject, 'view')
        body = self.body()
        self.assertNotIn('e' * 64, body, 'the manifest checksum leaked into the panel')
        self.assertNotIn('d' * 64, body, 'the full digest leaked, only the short form belongs here')

    def test_the_revision_history_moved_to_its_own_tab(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        # Linked from the detail page as a tab, and rendering the history itself.
        self.assertIn(url, self.body())
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        self.assertIn('d' * 12, response.content.decode())

    def test_the_history_tab_leads_with_created_then_the_linked_digest(self):
        # The linked column is a table's way into the detail page, so it follows the timestamp.
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        table = self.client.get(url).context['table']
        self.assertEqual([column.name for column in table.columns][:3], ['created', 'short_digest', 'status'])

    def test_the_history_tab_shows_the_script_file_count(self):
        # Asserted on the configured table, because a declared column survives being dropped
        # from the displayed set and would still answer get_cell().
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        response = self.client.get(url)
        visible = [name for name, _label in response.context['table'].selected_columns]

        self.assertIn('script_file_count', visible)

    def test_the_history_tab_links_each_revision(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        body = self.client.get(url).content.decode()
        self.assertIn(self.revision.get_absolute_url(), body)

    def test_a_staging_with_no_digest_is_named_and_still_linked(self):
        # The row a reader most wants to open, since a rejected staging stored nothing.
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        rejected = ScriptProjectRevision.objects.create(
            project=self.project,
            digest=None,
            status=RevisionStatusChoices.INVALID,
            validation_errors=[{'path': 'notes.txt', 'code': 'not_a_python_file', 'message': 'Refused.'}],
        )
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        body = self.client.get(url).content.decode()
        self.assertIn('Not stored', body)
        self.assertIn(rejected.get_absolute_url(), body)

    def test_the_tabs_are_withheld_without_the_revision_view_permission(self):
        self.grant(ScriptProject, 'view')

        body = self.body()

        for name in ('revisions', 'files'):
            url = reverse(f'plugins:netbox_scripts:scriptproject_{name}', args=[self.project.pk])
            self.assertNotIn(url, body)

    def test_the_history_tab_hides_a_revision_the_grant_excludes(self):
        # Core restricts every ObjectChildrenView's children, and this tab did not.
        other = ScriptProjectRevision.objects.create(
            project=self.project,
            digest='c' * 64,
            status=RevisionStatusChoices.VALID,
        )
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view', constraints={'pk': self.revision.pk})
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])

        body = self.client.get(url).content.decode()

        self.assertIn('d' * 12, body)
        self.assertNotIn('c' * 12, body)
        self.assertNotIn(other.get_absolute_url(), body)

    def test_the_history_tab_offers_no_actions_on_a_revision(self):
        # A revision is never created or edited by hand, so the tab carries no action buttons.
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        url = reverse('plugins:netbox_scripts:scriptproject_revisions', args=[self.project.pk])
        body = self.client.get(url).content.decode()
        for absent in ('scriptprojectrevision_add', 'scriptprojectrevision_edit'):
            self.assertNotIn(absent, body)

    def test_a_project_with_no_source_renders(self):
        self.grant(ScriptProject, 'view')
        bare = ScriptProject.objects.create(name='Bare State', key='bare-state')
        response = self.client.get(bare.get_absolute_url())
        self.assertHttpStatus(response, 200)
        self.assertIn('No source', response.content.decode())

    def test_the_add_script_action_links_to_the_upload_route(self):
        # Both halves: an upload creates a Script File, so the route needs that permission too.
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'add')
        expected = reverse('plugins:netbox_scripts:scriptproject_add_script', args=[self.project.pk])
        self.assertIn(expected, self.body())

    def test_the_add_script_action_is_inert_without_the_script_file_add_permission(self):
        # permissions_required cannot name another model, so without this check the button would
        # link somewhere the view refuses once a file has already been chosen.
        self.grant(ScriptProject, 'view', 'change')
        body = self.body()
        expected = reverse('plugins:netbox_scripts:scriptproject_add_script', args=[self.project.pk])

        self.assertIn('Add Script', body)
        self.assertNotIn(expected, body)
        self.assertIn('Script File add permission', body)

    def test_the_add_script_action_is_hidden_without_the_change_permission(self):
        # The Script File half is granted so the inert branch cannot satisfy the assertion for us.
        self.grant(ScriptProject, 'view')
        self.grant(ScriptFile, 'add')
        expected = reverse('plugins:netbox_scripts:scriptproject_add_script', args=[self.project.pk])
        self.assertNotIn(expected, self.body())

    def synchronized_project(self):
        source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        return ScriptProject.objects.create(
            name='Synced State',
            key='synced-state',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='scripts',
        )

    def test_the_add_script_action_is_absent_on_a_data_source_project(self):
        # Ingestion refuses an upload into a synchronized project, so offering the button would
        # put a user on a path that can only fail. The Script File half is granted for the same reason
        # as the test above.
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'add')
        synced = self.synchronized_project()
        expected = reverse('plugins:netbox_scripts:scriptproject_add_script', args=[synced.pk])
        self.assertNotIn(expected, self.client.get(synced.get_absolute_url()).content.decode())

    def test_uploading_into_a_data_source_project_is_a_form_error(self):
        # A hand-typed URL still reaches the view, and the refusal has to be a form error. Left to
        # ingestion it surfaces out of form.save(), which ObjectEditView does not catch.
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'add')
        synced = self.synchronized_project()
        url = reverse('plugins:netbox_scripts:scriptproject_add_script', args=[synced.pk])
        response = self.client.post(url, {'upload_file': SimpleUploadedFile('deploy.py', b'X = 1\n')})
        self.assertHttpStatus(response, 200)
        self.assertIn('reconciled from its Data Source rather than uploaded', response.content.decode())
        self.assertFalse(ScriptProjectRevision.objects.filter(project=synced).exists())


@override_settings(STORAGES=ACTIVATE_STORAGES)
class ScriptProjectActivateViewTestCase(ObjectPermissionTestMixin, TestCase):
    """Manual activation, which a project whose policy is manual has no other route to."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = ScriptProject.objects.create(name='Manual Project', key='manual-project')
        self.revision = service.stage_revision(self.project, {'deploy.py': b'VALUE = 1\n'}).revision
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(status=RevisionStatusChoices.VALID)
        self.revision.refresh_from_db()

    def url(self):
        return reverse('plugins:netbox_scripts:scriptproject_activate', args=[self.project.pk])

    def grant(self, *actions):
        return super().grant(ScriptProject, *actions)

    def grant_scripts(self, *actions):
        obj_perm = ObjectPermission(name=f'script {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(NetBoxScript))

    def test_the_candidate_is_the_newest_validated_revision(self):
        self.assertEqual(self.project.activatable_revision(), self.revision)

    def test_the_confirmation_names_the_revision_that_would_go_live(self):
        self.grant('view', 'activate')
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        self.assertIn(self.revision.short_digest, response.content.decode())

    def test_posting_activates_the_revision(self):
        self.grant('view', 'activate')
        response = self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.assertHttpStatus(response, 302)
        self.revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(self.revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, self.revision.pk)

    def test_an_already_current_project_offers_nothing(self):
        self.grant('view', 'activate')
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.project.refresh_from_db()
        # The active revision is excluded, so the button disappears rather than re-activating.
        self.assertIsNone(self.project.activatable_revision())
        self.assertIn('no validated revision', self.client.get(self.url()).content.decode())

    def test_a_project_with_nothing_valid_is_refused_rather_than_erroring(self):
        self.grant('view', 'activate')
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(status=RevisionStatusChoices.MATERIALIZED)
        response = self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.assertHttpStatus(response, 302)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_the_button_appears_only_when_there_is_something_to_activate(self):
        self.grant('view', 'activate')
        detail = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(self.url(), detail)

        self.client.post(self.url(), {'revision_id': self.revision.pk})
        detail = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertNotIn(self.url(), detail)

    def test_the_view_permission_alone_is_not_enough(self):
        self.grant('view')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def delete_project_url(self):
        return reverse('plugins:netbox_scripts:scriptproject_delete', args=[self.project.pk])

    def test_the_delete_page_renders_for_a_project_with_an_active_revision(self):
        # The confirmation page runs the deletion collector before deleting anything. With the
        # pointer declared PROTECT, that raised and the page refused, naming the project as its
        # own dependent object, so an activated project could not be deleted through the UI.
        self.grant('view', 'activate', 'delete')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.assertHttpStatus(self.client.get(self.delete_project_url()), 200)

    def test_deleting_an_active_project_through_the_ui_succeeds(self):
        # Also covers the second half: cascading the revisions away queues delete events, which
        # serialize eagerly, so the revision needs a serializer resolvable by model name.
        self.grant('view', 'activate', 'delete')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        response = self.client.post(self.delete_project_url(), {'confirm': True})
        self.assertHttpStatus(response, 302)
        self.assertFalse(ScriptProject.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(ScriptProjectRevision.objects.filter(pk=self.revision.pk).exists())
        self.assertFalse(NetBoxScript.objects.exists())

    def test_bulk_deleting_an_active_project_succeeds(self):
        self.grant('view', 'activate', 'delete')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        bulk = reverse('plugins:netbox_scripts:scriptproject_bulk_delete')
        response = self.client.post(bulk, {'pk': [self.project.pk], 'confirm': True, '_confirm': True})
        self.assertHttpStatus(response, 302)
        self.assertFalse(ScriptProject.objects.filter(pk=self.project.pk).exists())

    def publish(self):
        """Record one Script on the revision, so activation has something to publish."""
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(
            discovered_scripts=[
                {
                    'module_path': 'deploy',
                    'class_name': 'Deploy',
                    'script_file_id': 1,
                    'script_file_path': 'deploy.py',
                    'position': 0,
                    'display_name': 'Deploy',
                    'description': '',
                    'metadata': {},
                }
            ]
        )
        self.revision.refresh_from_db()

    def script_changes(self):
        """Count the change-log entries recorded against Scripts."""
        return ObjectChange.objects.filter(changed_object_type=ObjectType.objects.get_for_model(NetBoxScript)).count()

    def test_the_success_message_reports_what_this_route_published(self):
        # The wording lives in one builder shared with the Revisions tab, but this route's
        # wiring to it is its own thing to break.
        self.grant('view', 'activate')
        self.publish()
        response = self.client.post(self.url(), {'revision_id': self.revision.pk}, follow=True)

        self.assertIn('publishing 1 Script.', str(list(response.context['messages'])[0]))

    def test_a_revision_publishing_nothing_says_so_on_this_route_too(self):
        # The fixture records no discovered scripts, which is a real and easily misread state.
        self.grant('view', 'activate')
        response = self.client.post(self.url(), {'revision_id': self.revision.pk}, follow=True)

        self.assertIn('publishes no Scripts', str(list(response.context['messages'])[0]))

    def test_activating_through_the_view_publishes_scripts_and_logs_the_change(self):
        # A request-bound write reverses the model's own routes during event serialization, so
        # this is also the proof that the identity surface holds up under a real request.
        self.grant('view', 'activate')
        self.publish()
        response = self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.assertHttpStatus(response, 302)
        self.assertTrue(NetBoxScript.objects.filter(project=self.project, class_name='Deploy').exists())
        self.assertGreater(self.script_changes(), 0)

    def scripts_panel_url(self):
        """The list route the detail page's scripts panel fetches over HTMX, filtered to it."""
        return f'{reverse("plugins:netbox_scripts:netboxscript_list")}?project_id={self.project.pk}'

    def test_the_project_page_fetches_its_scripts_panel(self):
        # The panel renders a card and an hx-get rather than rows, so the page carries the
        # filtered URL and the fetch behind it is asserted separately below.
        self.grant('view', 'activate')
        self.grant_scripts('view')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(f'/plugins/netbox-scripts/scripts/?embedded=True&project_id={self.project.pk}', body)

    def test_the_fetched_panel_lists_each_published_script(self):
        # Following the hx-get is what proves the panel reaches the scripts, and it covers
        # more than the old inline markup did: the filter, the route, and the row link.
        self.grant('view', 'activate')
        self.grant_scripts('view')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        script = NetBoxScript.objects.get(project=self.project)

        response = self.client.get(self.scripts_panel_url())
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertIn(script.get_absolute_url(), body)
        self.assertIn(script.display_name, body)

    def test_the_panel_renders_for_a_project_with_no_scripts(self):
        # An empty project still gets the card. The empty state itself is NetBox's standard
        # table one, rendered by the fetch rather than inline.
        self.grant('view', 'activate')
        self.grant_scripts('view')
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(f'/plugins/netbox-scripts/scripts/?embedded=True&project_id={self.project.pk}', body)

    def test_the_panel_is_hidden_without_permission_to_view_scripts(self):
        # should_render() drops the whole card, so the fetch URL is absent rather than the
        # panel rendering empty.
        self.grant('view', 'activate')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertNotIn(f'/plugins/netbox-scripts/scripts/?embedded=True&project_id={self.project.pk}', body)

    def test_reactivating_the_same_revision_logs_nothing_further(self):
        # The user-visible form of never saving an unchanged row. The view offers no candidate
        # once the revision is active, so the repair path is driven directly, inside a request
        # context because the change-log receiver bails without one.
        self.grant('view', 'activate')
        self.publish()
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        before = self.script_changes()
        self.assertGreater(before, 0)

        request = RequestFactory().post(self.url())
        request.id = uuid.uuid4()
        request.user = self.user
        with event_tracking(request):
            activation.activate_revision(self.revision)
        self.assertEqual(self.script_changes(), before)

    def test_a_confirmation_remains_bound_to_the_revision_shown(self):
        self.grant('view', 'activate')
        response = self.client.get(self.url())
        self.assertContains(response, f'value="{self.revision.pk}"')
        newer = service.stage_revision(self.project, {'deploy.py': b'VALUE = 2\n'}).revision
        ScriptProjectRevision.objects.filter(pk=newer.pk).update(status=RevisionStatusChoices.VALID)
        self.client.post(self.url(), {'revision_id': self.revision.pk})
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, self.revision.pk)

    def test_posting_without_a_revision_does_not_choose_one(self):
        self.grant('view', 'activate')
        self.client.post(self.url(), {})
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_a_revision_from_another_project_is_refused(self):
        self.grant('view', 'activate')
        other = ScriptProject.objects.create(name='Other confirmation', key='other-confirmation')
        revision = service.stage_revision(other, {'deploy.py': b'VALUE = 3\n'}).revision
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        self.client.post(self.url(), {'revision_id': revision.pk})
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_an_ineligible_status_is_refused_before_the_activation_service(self):
        # The second revision is what makes this bite: without the form the view resolves its own
        # newest candidate and activates that one, so the refusal has to name the confirmed one.
        self.grant('view', 'activate')
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(status=RevisionStatusChoices.INVALID)
        other = service.stage_revision(self.project, {'deploy.py': b'VALUE = 2\n'}).revision
        ScriptProjectRevision.objects.filter(pk=other.pk).update(status=RevisionStatusChoices.VALID)
        with mock.patch('netbox_scripts.activation.activate_revision') as activate:
            response = self.client.post(self.url(), {'revision_id': self.revision.pk}, follow=True)
        activate.assert_not_called()
        self.assertContains(response, 'The confirmed revision can no longer be activated.')

    def test_a_project_with_no_candidate_reports_that_rather_than_a_bad_revision(self):
        # The commonest refusal on this route. Reporting it as missing or foreign would describe
        # something that did not happen.
        self.grant('view', 'activate')
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(status=RevisionStatusChoices.MATERIALIZED)
        response = self.client.post(self.url(), {}, follow=True)
        self.assertContains(response, 'no validated revision')


@override_settings(STORAGES=ACTIVATE_STORAGES)
class ScriptProjectRepairViewTestCase(ObjectPermissionTestMixin, TestCase):
    """Republishing a serving Project's rows, the one route to the already-active path."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = ScriptProject.objects.create(name='Repair Project', key='repair-project')
        self.revision = service.stage_revision(self.project, {'deploy.py': b'VALUE = 1\n'}).revision
        ScriptProjectRevision.objects.filter(pk=self.revision.pk).update(
            status=RevisionStatusChoices.VALID,
            discovered_scripts=[
                {
                    'module_path': 'deploy',
                    'class_name': 'Deploy',
                    'script_file_id': 1,
                    'script_file_path': 'deploy.py',
                    'position': 0,
                    'display_name': 'Deploy',
                    'description': '',
                    'metadata': {},
                }
            ],
        )
        self.revision.refresh_from_db()
        activation.activate_revision(self.revision)
        self.project.refresh_from_db()

    def url(self, project=None):
        return reverse('plugins:netbox_scripts:scriptproject_repair', args=[(project or self.project).pk])

    def grant(self, *actions, constraints=None):
        return super().grant(ScriptProject, *actions, constraints=constraints)

    def message(self, response):
        return str(list(response.context['messages'])[0])

    def test_the_action_links_to_the_repair_route(self):
        self.grant('view', 'activate')
        self.assertIn(self.url(), self.client.get(self.project.get_absolute_url()).content.decode())

    def test_the_action_is_inert_rather_than_hidden_for_a_project_serving_nothing(self):
        # Hiding it would leave an operator hunting for a button that used to be there.
        self.grant('view', 'activate')
        idle = ScriptProject.objects.create(name='Idle', key='idle')
        body = self.client.get(idle.get_absolute_url()).content.decode()

        self.assertIn('Repair Scripts', body)
        self.assertNotIn(self.url(idle), body)
        self.assertIn('serving no revision', body)

    def test_a_get_confirms_and_names_the_revision(self):
        self.grant('view', 'activate')
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        self.assertIn(self.revision.short_digest, response.content.decode())

    def test_posting_recreates_a_row_that_went_missing_and_reports_the_count(self):
        self.grant('view', 'activate')
        NetBoxScript.objects.filter(project=self.project).delete()
        response = self.client.post(self.url(), follow=True)

        self.assertTrue(NetBoxScript.objects.filter(project=self.project, class_name='Deploy').exists())
        self.assertIn('Repaired 1 Script', self.message(response))

    def test_posting_with_nothing_wrong_says_so_instead_of_claiming_a_repair(self):
        self.grant('view', 'activate')
        response = self.client.post(self.url(), follow=True)

        self.assertIn('already matched its revision', self.message(response))
        self.assertNotIn('Repaired', self.message(response))

    def test_the_change_permission_alone_is_not_enough(self):
        self.grant('view', 'change')
        self.assertHttpStatus(self.client.get(self.url()), 403)
        self.assertHttpStatus(self.client.post(self.url()), 403)

    def test_a_project_serving_nothing_has_no_reachable_route(self):
        # The queryset excludes it, so a hand-typed URL is a 404 rather than an error later.
        self.grant('view', 'activate')
        idle = ScriptProject.objects.create(name='Idle', key='idle')
        self.assertHttpStatus(self.client.get(self.url(idle)), 404)

    def test_an_object_constraint_narrows_the_route(self):
        self.grant('view', 'activate', constraints={'key': 'somebody-elses-project'})
        self.assertHttpStatus(self.client.get(self.url()), 404)
