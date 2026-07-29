from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from core.models import ObjectType
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.ingestion import ingest_upload
from netbox_custom_scripts.jobs import RevisionValidationJob
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_custom_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

SCRIPT = b'from netbox_custom_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class CustomScriptProjectUploadViewTestCase(TestCase):
    """The upload view creates a Project from one script and declares its entrypoint."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    @staticmethod
    def url():
        return reverse('plugins:netbox_custom_scripts:customscriptproject_upload')

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def grant_both(self):
        # The view creates a Project and declares its entrypoint, so it needs both.
        self.grant(CustomScriptProject, 'view', 'add')
        self.grant(CustomScriptModule, 'view', 'add')

    @staticmethod
    def upload(name='deploy.py', content=SCRIPT):
        return SimpleUploadedFile(name, content, content_type='text/x-python')

    def post(self, **overrides):
        data = {
            'name': 'Deploy Devices',
            'key': 'deploy-devices',
            'upload_file': self.upload(),
        }
        data.update(overrides)
        return self.client.post(self.url(), data)

    def test_the_form_asks_for_nothing_internal(self):
        self.grant_both()
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        for internal in ('source_path', 'entrypoint_digest', 'storage_key', 'digest', 'manifest'):
            self.assertNotIn(f'name="{internal}"', body)

    def test_an_upload_creates_the_project_the_declaration_and_the_revision(self):
        self.grant_both()
        response = self.post()
        self.assertHttpStatus(response, 302)

        project = CustomScriptProject.objects.get(key='deploy-devices')
        # Straight to the Project, which is where the source state and the entrypoints are.
        self.assertEqual(response.url, project.get_absolute_url())
        self.assertEqual(project.source_type, ProjectSourceTypeChoices.UPLOAD)
        module = CustomScriptModule.objects.get(project=project)
        self.assertEqual(module.source_path, 'deploy.py')
        self.assertTrue(module.enabled)
        revision = CustomScriptProjectRevision.objects.get(project=project)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.enqueued.assert_called_once()

    def test_the_checkbox_selects_automatic_activation(self):
        self.grant_both()
        self.assertHttpStatus(self.post(validate_and_activate='on'), 302)
        project = CustomScriptProject.objects.get(key='deploy-devices')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.AUTOMATIC_IF_VALID)

    def test_leaving_the_checkbox_clear_keeps_activation_manual(self):
        self.grant_both()
        self.assertHttpStatus(self.post(), 302)
        project = CustomScriptProject.objects.get(key='deploy-devices')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.MANUAL)

    def test_a_non_python_upload_is_refused_on_the_field(self):
        self.grant_both()
        response = self.client.post(
            self.url(),
            {'name': 'Notes', 'key': 'notes', 'upload_file': self.upload('notes.txt', b'hello\n')},
        )
        self.assertHttpStatus(response, 200)
        self.assertIn('Python source', response.content.decode())
        self.assertFalse(CustomScriptProject.objects.exists())
        self.enqueued.assert_not_called()

    def test_a_traversing_file_name_lands_flat_inside_the_project(self):
        # Django reduces every uploaded name to its basename before the form sees it, so a
        # traversing name arrives already flattened and is stored as an ordinary file. The path
        # policy still guards ingest_upload, which Data Source sync will reach with real paths.
        self.grant_both()
        self.assertHttpStatus(self.post(upload_file=self.upload('../escape.py')), 302)
        self.assertEqual(CustomScriptModule.objects.get().source_path, 'escape.py')

    def test_a_missing_file_is_refused(self):
        self.grant_both()
        response = self.client.post(self.url(), {'name': 'Deploy Devices', 'key': 'deploy-devices'})
        self.assertHttpStatus(response, 200)
        self.assertFalse(CustomScriptProject.objects.exists())

    def test_a_nested_file_name_is_reduced_to_its_basename(self):
        # An upload can therefore never create a nested entrypoint. Nesting reaches a project
        # through its Data Source directory instead.
        self.grant_both()
        self.assertHttpStatus(self.post(upload_file=self.upload('automation/deploy.py')), 302)
        self.assertEqual(CustomScriptModule.objects.get().source_path, 'deploy.py')

    def test_the_project_permission_alone_is_not_enough(self):
        # The upload declares an entrypoint, so it needs the Module permission too.
        self.grant(CustomScriptProject, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_module_permission_alone_is_not_enough(self):
        self.grant(CustomScriptModule, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class CustomScriptProjectAddScriptViewTestCase(TestCase):
    """Adding a second script stages the existing tree plus the new file."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )
        self.project = CustomScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT).revision
        # The fixture enqueued once, so the assertions below count only what the request did.
        self.enqueued.reset_mock()

    def url(self):
        return reverse('plugins:netbox_custom_scripts:customscriptproject_add_script', args=[self.project.pk])

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def grant_both(self):
        # Registered on the detail route, so the base view asks for change, not add.
        self.grant(CustomScriptProject, 'view', 'change')
        self.grant(CustomScriptModule, 'view', 'add')

    @staticmethod
    def upload(name='audit.py', content=SCRIPT + b'# audit\n'):
        return SimpleUploadedFile(name, content, content_type='text/x-python')

    def test_a_second_script_joins_the_existing_tree(self):
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload()})
        self.assertHttpStatus(response, 302)

        revision = CustomScriptProjectRevision.objects.exclude(pk=self.first.pk).get()
        self.assertEqual(sorted(entry['path'] for entry in revision.manifest), ['audit.py', 'deploy.py'])
        # Both are entrypoints, since an uploaded file always is.
        self.assertEqual(
            sorted(entry['source_path'] for entry in revision.entrypoint_snapshot),
            ['audit.py', 'deploy.py'],
        )
        self.enqueued.assert_called_once()

    def test_the_earlier_declaration_survives(self):
        self.grant_both()
        self.client.post(self.url(), {'upload_file': self.upload()})
        self.assertEqual(
            sorted(self.project.modules.filter(enabled=True).values_list('source_path', flat=True)),
            ['audit.py', 'deploy.py'],
        )

    def test_replacing_a_known_path_needs_the_confirmation(self):
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload('deploy.py')})
        self.assertHttpStatus(response, 200)
        self.assertIn('already holds', response.content.decode())
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 1)
        self.enqueued.assert_not_called()

    def test_the_confirmation_allows_the_replacement_as_a_new_revision(self):
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload('deploy.py'), 'confirm_replace': 'on'})
        self.assertHttpStatus(response, 302)
        revision = CustomScriptProjectRevision.objects.exclude(pk=self.first.pk).get()
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.assertNotEqual(revision.digest, self.first.digest)
        # The old revision is untouched, so the replaced content stays recoverable.
        self.first.refresh_from_db()
        self.assertEqual([entry['path'] for entry in self.first.manifest], ['deploy.py'])

    def test_two_names_sharing_a_basename_trigger_the_confirmation(self):
        # The whole point of keying on the canonical path: the user picked a different name, but
        # Django flattened it onto one the project already holds.
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload('archive/deploy.py')})
        self.assertHttpStatus(response, 200)
        self.assertIn('already holds', response.content.decode())
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 1)

    def test_a_case_variant_is_refused_on_the_upload_field(self):
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload('Deploy.py')})
        self.assertHttpStatus(response, 200)
        self.assertIn('collides', response.content.decode())
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 1)

    def test_a_non_python_upload_is_refused(self):
        self.grant_both()
        response = self.client.post(self.url(), {'upload_file': self.upload('notes.txt', b'hello\n')})
        self.assertHttpStatus(response, 200)
        self.assertIn('Python source', response.content.decode())

    def test_the_add_permission_is_not_what_this_route_needs(self):
        # ObjectEditView derives the action from the URL, so a detail route asks for change.
        self.grant(CustomScriptProject, 'view', 'add')
        self.grant(CustomScriptModule, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_module_permission_alone_is_not_enough(self):
        self.grant(CustomScriptModule, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)
