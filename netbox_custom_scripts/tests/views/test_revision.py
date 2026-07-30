from django.test import override_settings
from django.urls import reverse

from core.models import ObjectType
from netbox_custom_scripts import activation
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage import service
from netbox_custom_scripts.storage.exceptions import ActivationError
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user

REVISION_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_custom_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


def record(class_name='Deploy'):
    """One snapshot record, the shape runtime introspection produces."""
    return {
        'module_path': 'deploy',
        'class_name': class_name,
        'entrypoint_module_id': 1,
        'entrypoint_path': 'deploy.py',
        'position': 0,
        'display_name': class_name,
        'description': '',
        'metadata': {},
    }


@override_settings(STORAGES=REVISION_STORAGES)
class RevisionServiceViewTestCase(TestCase):
    """The per-row Activate and Deactivate buttons on a project's Revisions tab."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = CustomScriptProject.objects.create(name='Serviced', key='serviced')

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    counter = 0

    def valid_revision(self, records=None):
        """Stage distinct content, mark it valid, and record what it publishes."""
        type(self).counter += 1
        revision = service.stage_revision(self.project, {'deploy.py': f'V = {self.counter}\n'.encode()}).revision
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALID,
            discovered_scripts=[record()] if records is None else records,
        )
        revision.refresh_from_db()
        return revision

    @staticmethod
    def url(revision, action):
        return reverse(f'plugins:netbox_custom_scripts:customscriptprojectrevision_{action}', args=[revision.pk])

    def tab_url(self):
        return f'{self.project.get_absolute_url()}revisions/'

    def test_activating_puts_the_revision_into_service(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        response = self.client.post(self.url(revision, 'activate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertTrue(CustomScript.objects.get(project=self.project).is_executable)

    def test_deactivating_retires_the_revision_and_its_scripts(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        response = self.client.post(self.url(revision, 'deactivate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.RETIRED)
        self.assertIsNone(self.project.active_revision_id)
        self.assertTrue(CustomScript.objects.get(project=self.project).is_retired)

    def test_reactivating_brings_the_same_rows_back(self):
        # The point of retiring rather than deleting: the primary key and the administrator's
        # enabled both survive a round trip through deactivation.
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        script = CustomScript.objects.get(project=self.project)
        CustomScript.objects.filter(pk=script.pk).update(enabled=False)

        self.client.post(self.url(revision, 'deactivate'))
        self.client.post(self.url(revision, 'activate'))

        returned = CustomScript.objects.get(project=self.project)
        self.assertEqual(returned.pk, script.pk)
        self.assertFalse(returned.is_retired)
        self.assertFalse(returned.enabled)

    def test_deactivating_a_revision_that_is_not_active_is_refused(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        response = self.client.post(self.url(revision, 'deactivate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_activating_a_second_revision_retires_the_first(self):
        self.grant(CustomScriptProject, 'view', 'change')
        first = self.valid_revision()
        self.client.post(self.url(first, 'activate'))
        second = self.valid_revision(records=[record(class_name='Later')])
        self.client.post(self.url(second, 'activate'))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)

    def test_a_get_goes_back_to_the_tab_without_changing_anything(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        response = self.client.get(self.url(revision, 'activate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_the_project_view_permission_alone_is_not_enough(self):
        self.grant(CustomScriptProject, 'view')
        revision = self.valid_revision()
        self.assertHttpStatus(self.client.post(self.url(revision, 'activate')), 403)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_no_revision_permission_of_its_own_is_needed(self):
        # The operation changes what the project serves, so the project's permission is the
        # gate. A revision has no other surface an operator would grant a permission for.
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        self.assertHttpStatus(self.client.post(self.url(revision, 'activate')), 302)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)

    def test_the_tab_offers_activate_for_a_valid_revision_and_nothing_else(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        body = self.client.get(self.tab_url()).content.decode()
        self.assertIn(self.url(revision, 'activate'), body)
        self.assertNotIn(self.url(revision, 'deactivate'), body)

    def test_the_tab_offers_deactivate_for_the_active_revision(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        body = self.client.get(self.tab_url()).content.decode()
        self.assertIn(self.url(revision, 'deactivate'), body)
        self.assertNotIn(self.url(revision, 'activate'), body)

    def test_the_tab_offers_neither_button_without_the_change_permission(self):
        self.grant(CustomScriptProject, 'view')
        revision = self.valid_revision()
        body = self.client.get(self.tab_url()).content.decode()
        self.assertNotIn(self.url(revision, 'activate'), body)

    def test_a_materialized_revision_offers_neither_button(self):
        self.grant(CustomScriptProject, 'view', 'change')
        revision = service.stage_revision(self.project, {'deploy.py': b'V = 99\n'}).revision
        self.assertFalse(revision.is_activatable)
        body = self.client.get(self.tab_url()).content.decode()
        self.assertNotIn(self.url(revision, 'activate'), body)
        self.assertNotIn(self.url(revision, 'deactivate'), body)


@override_settings(STORAGES=REVISION_STORAGES)
class DeactivateRevisionTestCase(TestCase):
    """The domain operation behind the button."""

    def setUp(self):
        self.project = CustomScriptProject.objects.create(name='Domain', key='domain')

    def activated(self):
        revision = service.stage_revision(self.project, {'deploy.py': b'V = 1\n'}).revision
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALID, discovered_scripts=[record()]
        )
        revision.refresh_from_db()
        activation.activate_revision(revision)
        revision.refresh_from_db()
        return revision

    def test_it_returns_the_retired_revision(self):
        revision = self.activated()
        self.assertEqual(activation.deactivate_revision(revision).status, RevisionStatusChoices.RETIRED)

    def test_it_refuses_a_revision_that_is_not_active(self):
        revision = self.activated()
        activation.deactivate_revision(revision)
        with self.assertRaises(ActivationError):
            activation.deactivate_revision(revision)

    def test_it_leaves_the_revision_activatable_again(self):
        revision = self.activated()
        activation.deactivate_revision(revision)
        revision.refresh_from_db()
        self.assertTrue(revision.is_activatable)
        self.assertFalse(revision.is_active)
