from unittest import mock

from django.test import override_settings
from django.urls import reverse

from core.models import DataSource, ObjectType
from netbox.registry import registry
from netbox_scripts.choices import ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.jobs import ProjectReconciliationJob
from netbox_scripts.models import NetBoxScript, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import service
from users.models import ObjectPermission
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
class SourceManagementPermissionTestCase(TestCase):
    """
    Each source-management surface gated on the permission that names it, not a borrowed one.

    The negative cases are the point: someone who may rename a project must not thereby be able to
    change what it serves. Run and schedule are covered in the run suites, next to their fixture.
    """

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

    def grant(self, *actions, model=ScriptProject, constraints=None):
        """Grant the named actions on one model to the test user."""
        permission = ObjectPermission(
            name=f'{model._meta.model_name} {"/".join(actions)}',
            actions=list(actions),
            constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))

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

        self.assertHttpStatus(self.client.post(self.project_url('activate')), 403)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activate_alone_can_activate_a_project(self):
        self.grant('view', 'activate')

        self.assertHttpStatus(self.client.post(self.project_url('activate')), 302)
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

        self.assertHttpStatus(self.client.post(self.project_url('activate')), 404)
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
