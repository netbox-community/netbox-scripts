from unittest import mock

from django.test import override_settings
from django.urls import reverse

from core.models import DataSource, ObjectType
from netbox.registry import registry
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.jobs import ProjectReconciliationJob
from netbox_scripts.models import NetBoxScript, ScriptProject, ScriptProjectRevision
from netbox_scripts.permissions import AUTHORIZED_SOURCE_MOVES, moved_source_fields, unpermitted_source_moves
from netbox_scripts.storage import service
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin
from utilities.permissions import get_permission_for_model
from utilities.testing import TestCase, create_test_user

PERMISSION_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

# Every action the plugin declares. The legacy reference migration maps onto these.
DECLARED = {
    ScriptProject: ('view', 'add', 'change', 'delete', 'activate', 'migrate', 'reconcile'),
    NetBoxScript: ('view', 'change', 'run', 'schedule'),
}

# The actions declared in Meta.permissions, which is to say every one Django does not supply.
CUSTOM = {
    ScriptProject: ('activate', 'migrate', 'reconcile'),
    NetBoxScript: ('run', 'schedule'),
}

RECORD = {
    'module_path': 'deploy',
    'class_name': 'Deploy',
    'script_file_id': 1,
    'script_file_path': 'deploy.py',
    'position': 0,
    'display_name': 'Deploy',
    'description': '',
    'metadata': {},
}


@override_settings(STORAGES=PERMISSION_STORAGES)
class SourceFieldGateTestCase(ObjectPermissionTestMixin, TestCase):
    """
    The three fields that decide what a Project serves, gated on activate rather than change.

    The browser surfaces are here. REST is in api/test_projects.py, with that endpoint's contract.
    """

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.source = DataSource.objects.create(name='Repo', type='local', source_url='file:///tmp/repo/')
        self.other = DataSource.objects.create(name='Other', type='local', source_url='file:///tmp/other/')
        self.project = ScriptProject.objects.create(
            name='Synced',
            key='synced',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='scripts',
        )

    def grant(self, *actions, constraints=None):
        super().grant(ScriptProject, *actions, constraints=constraints)
        # restrict_form_fields narrows data_source to what the user may VIEW. Without this every
        # case below fails on the field instead of reaching the gate.
        super().grant(DataSource, 'view')

    def edit_post(self, **overrides):
        data = {
            'name': self.project.name,
            'key': self.project.key,
            'source_type': self.project.source_type,
            'data_source': self.source.pk,
            'data_path': self.project.data_path,
            'activation_policy': self.project.activation_policy,
            'enabled': 'on',
        }
        data.update(overrides)
        return self.client.post(reverse('plugins:netbox_scripts:scriptproject_edit', args=[self.project.pk]), data)

    def test_change_alone_cannot_move_a_field_on_the_edit_form(self):
        self.grant('view', 'change')

        response = self.edit_post(data_path='automation')

        self.assertEqual(response.status_code, 200)
        self.assertIn('data_path', response.context['form'].errors)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_activate_permits_the_edit(self):
        self.grant('view', 'change', 'activate')

        response = self.edit_post(data_path='automation')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'automation')

    def test_an_edit_that_moves_nothing_is_permitted(self):
        self.grant('view', 'change')

        response = self.edit_post(description='renamed only')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.description, 'renamed only')

    def test_an_edit_with_a_trailing_separator_is_not_a_move(self):
        """The form's cleaned_data is raw here: model clean() normalizes only after Form.clean()."""
        self.grant('view', 'change')

        response = self.edit_post(data_path='scripts/')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_activate_on_another_project_does_not_permit_the_move(self):
        # Permission rows are OR'd, so activate is granted only in its constrained form.
        self.grant('view', 'change')
        self.grant('activate', constraints={'key': 'somewhere-else'})

        response = self.edit_post(data_path='automation')

        self.assertEqual(response.status_code, 200)
        self.assertIn('data_path', response.context['form'].errors)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_activate_constrained_to_this_project_permits_the_move(self):
        self.grant('view', 'change')
        self.grant('activate', constraints={'key': self.project.key})

        response = self.edit_post(data_path='automation')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'automation')

    def test_a_constraint_is_read_from_the_stored_project(self):
        # Constrained to the current path, so the move out of it is permitted once, as documented.
        self.grant('view', 'change')
        self.grant('activate', constraints={'data_path': 'scripts'})

        response = self.edit_post(data_path='automation')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'automation')

    def bulk_edit_post(self, **fields):
        data = {'pk': [self.project.pk], '_apply': ''}
        data.update(fields)
        return self.client.post(reverse('plugins:netbox_scripts:scriptproject_bulk_edit'), data)

    def test_bulk_edit_refuses_a_changed_source_field(self):
        self.grant('view', 'change')

        self.bulk_edit_post(data_path='automation')

        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_bulk_edit_refuses_a_nullify_tick(self):
        """_nullify is the branch core takes instead of changed_data, so the form never sees it."""
        self.grant('view', 'change')

        response = self.bulk_edit_post(_nullify=['data_path'])

        self.assertIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_a_nullify_tick_with_activate_reaches_the_model_instead(self):
        """The model refuses an empty data path here, so the point is whose refusal it is."""
        self.grant('view', 'change', 'activate')

        response = self.bulk_edit_post(_nullify=['data_path'])

        self.assertNotIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def test_a_nullify_tick_outside_nullable_fields_is_not_a_move(self):
        """A Set null tick on a field outside nullable_fields is neither saved by core nor a move."""
        self.grant('view', 'change')
        ScriptProject.objects.filter(pk=self.project.pk).update(
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID
        )

        response = self.bulk_edit_post(_nullify=['activation_policy'])

        self.assertNotIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.activation_policy, ActivationPolicyChoices.AUTOMATIC_IF_VALID)

    def test_bulk_edit_ignores_activate_on_another_project(self):
        self.grant('view', 'change')
        self.grant('activate', constraints={'key': 'somewhere-else'})

        response = self.bulk_edit_post(data_path='automation')

        self.assertIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_path, 'scripts')

    def import_post(self, data):
        return self.client.post(
            reverse('plugins:netbox_scripts:scriptproject_bulk_import'),
            {'data': data, 'format': 'csv', 'csv_delimiter': 'auto'},
        )

    def test_bulk_import_refuses_a_move_on_an_update(self):
        """A record naming an existing id updates it, and data_source arrives as a name."""
        self.grant('view', 'add', 'change')

        response = self.import_post(f'id,name,data_source\n{self.project.pk},Synced,{self.other.name}\n')

        self.assertIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_source_id, self.source.pk)

    def test_bulk_import_treats_a_blank_cell_as_the_stored_value(self):
        """On an update, a blank cell for a field with a model default is the stored value, not a move."""
        self.grant('view', 'add', 'change')
        # A non-default policy, or a reset to the default would pass as "kept".
        ScriptProject.objects.filter(pk=self.project.pk).update(
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID
        )

        response = self.import_post(f'id,name,activation_policy\n{self.project.pk},Renamed,\n')

        self.assertNotIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.name, 'Renamed')
        self.assertEqual(self.project.activation_policy, ActivationPolicyChoices.AUTOMATIC_IF_VALID)

    def test_bulk_import_permits_a_create(self):
        """The create path is carved out, since there is no stored row to move away from."""
        self.grant('view', 'add')

        self.import_post(
            'name,key,source_type,data_source,data_path,activation_policy\n'
            f'Imported,imported,data_source,{self.source.name},automation,automatic_if_valid\n'
        )

        created = ScriptProject.objects.filter(key='imported').first()
        self.assertIsNotNone(created)
        self.assertEqual(created.activation_policy, 'automatic_if_valid')

    def test_an_unsaved_project_has_no_stored_row_to_move_from(self):
        """The carve-out the create path rests on. The view's pk test only saves a query."""
        submitted = {'activation_policy': 'automatic_if_valid', 'data_path': 'automation', 'data_source': self.source}

        self.assertEqual(moved_source_fields(None, submitted), ())

    def authorized(self, project):
        """What the gate recorded on an instance, or None when it never ran."""
        return getattr(project, AUTHORIZED_SOURCE_MOVES, None)

    def test_the_gate_records_the_move_it_authorized(self):
        # A site that forgot this call would reopen the hole in silence, with every existing test
        # still green, so it is pinned here rather than left implicit.
        self.grant('view', 'change', 'activate')
        submitted = {'data_path': 'automation'}

        self.assertEqual(unpermitted_source_moves(self.user, self.project, submitted), ())
        self.assertEqual(self.authorized(self.project), frozenset({'data_path'}))

    def test_the_gate_records_an_empty_set_when_nothing_moved(self):
        # The description-only case. An empty record still means a gate ran, which is what tells
        # the save to check at all.
        self.grant('view', 'change')

        self.assertEqual(unpermitted_source_moves(self.user, self.project, {}), ())
        self.assertEqual(self.authorized(self.project), frozenset())

    def test_a_refused_move_records_nothing(self):
        self.grant('view', 'change')

        self.assertEqual(unpermitted_source_moves(self.user, self.project, {'data_path': 'automation'}), ('data_path',))
        self.assertIsNone(self.authorized(self.project))

    def test_no_user_in_scope_records_nothing(self):
        # A write with no request behind it is not authorized against anything, so the save has
        # nothing to compare and leaves the field alone. Every fixture and service write is here.
        self.assertEqual(unpermitted_source_moves(None, self.project, {'data_path': 'automation'}), ())
        self.assertIsNone(self.authorized(self.project))

    def test_bulk_import_ignores_activate_on_another_project(self):
        self.grant('view', 'add', 'change')
        self.grant('activate', constraints={'key': 'somewhere-else'})

        response = self.import_post(f'id,name,data_source\n{self.project.pk},Synced,{self.other.name}\n')

        self.assertIn('requires the Script Project activate permission', response.content.decode())
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_source_id, self.source.pk)

    def test_bulk_import_permits_a_move_with_activate(self):
        self.grant('view', 'add', 'change', 'activate')

        response = self.import_post(f'id,name,data_source\n{self.project.pk},Synced,{self.other.name}\n')

        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.data_source_id, self.other.pk)


class SourceManagementPermissionTestCase(ObjectPermissionTestMixin, TestCase):
    """
    Each source-management surface gated on the permission that names it, not a borrowed one.

    The negative cases are the point: someone who may rename a project must not thereby be able to
    change what it serves. Run and schedule are covered in the run suites, next to their fixture.
    """

    def grant(self, *actions, model=ScriptProject, constraints=None):
        """Grant the named actions on one model to the test user."""
        return super().grant(model, *actions, constraints=constraints)

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.source = DataSource.objects.create(name='Repo', type='local', source_url='file:///tmp/repo/')
        self.project = ScriptProject.objects.create(name='Gated', key='gated')
        self.synchronized = ScriptProject.objects.create(
            name='Synced',
            key='synced',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='scripts',
        )
        self.enqueued = self.enterContext(
            mock.patch.object(ProjectReconciliationJob, 'enqueue_reconciliation', return_value=None)
        )
        self.revision = self.valid_revision()

    def valid_revision(self):
        """Stage content on the upload project and mark it valid, so it can be activated."""
        revision = service.stage_revision(self.project, {'deploy.py': b'V = 1\n'}).revision
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALID,
            discovered_scripts=[RECORD],
        )
        revision.refresh_from_db()
        return revision

    def project_url(self, action, project=None):
        return reverse(
            f'plugins:netbox_scripts:scriptproject_{action}',
            args=[(project or self.project).pk],
        )

    def revision_url(self, action):
        return reverse(f'plugins:netbox_scripts:scriptprojectrevision_{action}', args=[self.revision.pk])

    def test_change_alone_cannot_activate_a_project(self):
        # The regression this task exists to prevent: renaming is not activating.
        self.grant('view', 'change')

        self.assertHttpStatus(self.client.post(self.project_url('activate'), {'revision_id': self.revision.pk}), 403)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activate_alone_can_activate_a_project(self):
        self.grant('view', 'activate')

        self.assertHttpStatus(self.client.post(self.project_url('activate'), {'revision_id': self.revision.pk}), 302)
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, self.revision.pk)

    def test_change_alone_cannot_activate_a_revision(self):
        self.grant('view', 'change')

        self.assertHttpStatus(self.client.post(self.revision_url('activate')), 403)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.status, RevisionStatusChoices.VALID)

    def test_activate_alone_can_activate_a_revision(self):
        self.grant('view', 'activate')

        self.assertHttpStatus(self.client.post(self.revision_url('activate')), 302)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.status, RevisionStatusChoices.ACTIVE)

    def test_change_alone_cannot_reconcile_a_source(self):
        self.grant('view', 'change')

        self.assertHttpStatus(self.client.post(self.project_url('reconcile', self.synchronized)), 403)
        self.enqueued.assert_not_called()

    def test_reconcile_alone_can_reconcile_a_source(self):
        self.grant('view', 'reconcile')

        self.assertHttpStatus(self.client.post(self.project_url('reconcile', self.synchronized)), 302)
        self.enqueued.assert_called_once()

    def test_activate_does_not_confer_change(self):
        # The separation has to hold in both directions or it buys nothing.
        self.grant('view', 'activate')

        self.assertHttpStatus(self.client.get(self.project_url('edit')), 403)

    def test_an_object_constraint_narrows_which_projects_can_be_activated(self):
        # The constraint takes the object out of the restricted queryset, so 404 rather than 403.
        self.grant('view', 'activate', constraints={'key': 'somewhere-else'})

        self.assertHttpStatus(self.client.post(self.project_url('activate'), {'revision_id': self.revision.pk}), 404)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_a_constrained_activate_permission_narrows_revisions_too(self):
        # The revision views filter through the project queryset, which is where this bites.
        self.grant('view', 'activate', constraints={'key': 'somewhere-else'})

        self.assertHttpStatus(self.client.post(self.revision_url('activate')), 404)
        self.revision.refresh_from_db()
        self.assertEqual(self.revision.status, RevisionStatusChoices.VALID)

    def test_every_declared_action_has_its_permission_row(self):
        # Django composes f'{action}_{model}' for its own four and stores a Meta.permissions
        # codename verbatim, so a custom action's row carries the bare name. Pins the
        # declaration, not the migration, which --check covers. This is not what the permission
        # picker reads, so it is paired with the registry test below.
        for model, actions in DECLARED.items():
            registered = {
                permission.codename for permission in ObjectType.objects.get_for_model(model).permission_set.all()
            }
            custom = set(CUSTOM[model])
            for action in actions:
                expected = action if action in custom else f'{action}_{model._meta.model_name}'
                with self.subTest(model=model._meta.model_name, action=action):
                    self.assertIn(expected, registered)
                    if action in custom:
                        # create_permissions() never deletes, so a database that applied the old
                        # options keeps the compound row. This is what surfaces one.
                        self.assertNotIn(f'{action}_{model._meta.model_name}', registered)

    def test_the_picker_offers_the_action_the_views_ask_for(self):
        # The picker reads the action registry, stores the ticked name verbatim, and the backend
        # composes f'{app}.{action}_{model}' from it. A registered name carrying the model name
        # therefore grants run_netboxscript_netboxscript, which nothing checks, and the working
        # permission becomes reachable only through the form's free-text box.
        for model, actions in CUSTOM.items():
            label = f'{model._meta.app_label}.{model._meta.model_name}'
            offered = {action.name for action in registry['model_actions'][label]}
            for action in actions:
                with self.subTest(model=model._meta.model_name, action=action):
                    self.assertIn(action, offered)
                    self.assertNotIn(f'{action}_{model._meta.model_name}', offered)
                    self.assertEqual(
                        get_permission_for_model(model, action),
                        f'netbox_scripts.{action}_{model._meta.model_name}',
                    )
