from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from core.models import ObjectType
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.ingestion import ingest_upload
from netbox_scripts.jobs import RevisionValidationJob
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config
from netbox_scripts.storage.paths import STORAGE_PREFIX
from netbox_scripts.tests.storage.test_store import stored_paths
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

SCRIPT = b'from netbox_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ScriptProjectUploadViewTestCase(TestCase):
    """The upload view creates a Project from one script and declares its script file."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    @staticmethod
    def url():
        return reverse('plugins:netbox_scripts:scriptproject_upload')

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def grant_both(self):
        # The view creates a Project and declares its script file, so it needs both.
        self.grant(ScriptProject, 'view', 'add')
        self.grant(ScriptFile, 'view', 'add')

    @staticmethod
    def upload(name='deploy.py', content=SCRIPT):
        return SimpleUploadedFile(name, content, content_type='text/x-python')

    def post(self, **overrides):
        data = {
            'name': 'Deploy Devices',
            'key': 'deploy-devices',
            'upload_file': self.upload(),
            # The rendered select always submits its initial, so a minimal post here would be
            # less faithful than including it.
            'activation_policy': ActivationPolicyChoices.MANUAL,
        }
        data.update(overrides)
        # The form defers staging to the commit, which a TestCase never reaches on its own.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url(), data)

    def test_the_form_asks_for_nothing_internal(self):
        self.grant_both()
        response = self.client.get(self.url())
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        for internal in ('source_path', 'script_file_digest', 'storage_key', 'digest', 'manifest'):
            self.assertNotIn(f'name="{internal}"', body)

    def test_an_upload_creates_the_project_the_declaration_and_the_revision(self):
        self.grant_both()
        response = self.post()
        self.assertHttpStatus(response, 302)

        project = ScriptProject.objects.get(key='deploy-devices')
        # Straight to the Project, which is where the source state and the script files are.
        self.assertEqual(response.url, project.get_absolute_url())
        self.assertEqual(project.source_type, ProjectSourceTypeChoices.UPLOAD)
        script_file = ScriptFile.objects.get(project=project)
        self.assertEqual(script_file.source_path, 'deploy.py')
        self.assertTrue(script_file.enabled)
        revision = ScriptProjectRevision.objects.get(project=project)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.enqueued.assert_called_once()

    def test_the_standing_policy_field_is_what_the_project_persists(self):
        # The field that decides what later revisions do, which the tick beside it cannot set.
        self.grant_both()
        self.assertHttpStatus(self.post(activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID), 302)
        project = ScriptProject.objects.get(key='deploy-devices')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.AUTOMATIC_IF_VALID)

    def test_activating_this_upload_does_not_persist_an_automatic_policy(self):
        # The defect this split closes: the checkbox used to set the project's policy for life.
        self.grant_both()
        self.assertHttpStatus(self.post(activate_this_revision='on'), 302)
        project = ScriptProject.objects.get(key='deploy-devices')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.MANUAL)

    def test_the_standing_policy_field_offers_manual_first(self):
        # The safe default an operator has to opt out of, rather than opt in to.
        self.grant_both()
        form = self.client.get(self.url()).context['form']
        self.assertEqual(form.fields['activation_policy'].initial, ActivationPolicyChoices.MANUAL)
        self.assertTrue(form.fields['activate_this_revision'].initial)

    def test_the_one_shot_travels_to_the_validation_job(self):
        self.grant_both()
        self.assertHttpStatus(self.post(activate_this_revision='on'), 302)
        self.assertEqual(self.enqueued.call_args.kwargs['activate_once'], True)

    def test_no_one_shot_is_asked_for_when_the_box_is_clear(self):
        self.grant_both()
        self.assertHttpStatus(self.post(), 302)
        self.assertEqual(self.enqueued.call_args.kwargs['activate_once'], False)

    def test_a_non_python_upload_is_refused_on_the_field(self):
        self.grant_both()
        response = self.client.post(
            self.url(),
            {'name': 'Notes', 'key': 'notes', 'upload_file': self.upload('notes.txt', b'hello\n')},
        )
        self.assertHttpStatus(response, 200)
        self.assertIn('Python source', response.content.decode())
        self.assertFalse(ScriptProject.objects.exists())
        self.enqueued.assert_not_called()

    def test_a_traversing_file_name_lands_flat_inside_the_project(self):
        # Django reduces every uploaded name to its basename before the form sees it, so a
        # traversing name arrives already flattened and is stored as an ordinary file. The path
        # policy still guards ingest_upload, which Data Source sync will reach with real paths.
        self.grant_both()
        self.assertHttpStatus(self.post(upload_file=self.upload('../escape.py')), 302)
        self.assertEqual(ScriptFile.objects.get().source_path, 'escape.py')

    def test_a_missing_file_is_refused(self):
        self.grant_both()
        data = {
            'name': 'Deploy Devices',
            'key': 'deploy-devices',
            'activation_policy': ActivationPolicyChoices.MANUAL,
        }
        response = self.client.post(self.url(), data)
        self.assertHttpStatus(response, 200)
        # Named, so the absence of a project cannot be some other field's refusal.
        self.assertIn('upload_file', response.context['form'].errors)
        self.assertFalse(ScriptProject.objects.exists())

    def test_a_nested_file_name_is_reduced_to_its_basename(self):
        # An upload can therefore never create a nested script file. Nesting reaches a project
        # through its Data Source directory instead.
        self.grant_both()
        self.assertHttpStatus(self.post(upload_file=self.upload('automation/deploy.py')), 302)
        self.assertEqual(ScriptFile.objects.get().source_path, 'deploy.py')

    def test_a_rolled_back_upload_leaves_the_store_untouched(self):
        # A constraint the new project falls outside of makes the editing view raise
        # PermissionsViolation after the form saves, rolling the whole request back. Staging
        # waits for the commit, so nothing reaches the store.
        constrained = ObjectPermission(
            name='project add elsewhere',
            actions=['view', 'add'],
            constraints={'key': 'another-project'},
        )
        constrained.save()
        constrained.users.add(self.user)
        constrained.object_types.add(ObjectType.objects.get_for_model(ScriptProject))
        self.grant(ScriptFile, 'view', 'add')

        before = stored_paths(config.get_storage(), f'{STORAGE_PREFIX}/')
        response = self.post()

        self.assertHttpStatus(response, 200)
        self.assertFalse(ScriptProject.objects.exists())
        self.assertEqual(stored_paths(config.get_storage(), f'{STORAGE_PREFIX}/'), before)
        self.enqueued.assert_not_called()

    def test_the_project_permission_alone_is_not_enough(self):
        # The upload declares a script file, so it needs the Script File permission too.
        self.grant(ScriptProject, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_script_file_permission_alone_is_not_enough(self):
        self.grant(ScriptFile, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ScriptProjectAddScriptViewTestCase(TestCase):
    """Adding a second script stages the existing tree plus the new file."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT).revision
        # The fixture enqueued once, so the assertions below count only what the request did.
        self.enqueued.reset_mock()

    def url(self):
        return reverse('plugins:netbox_scripts:scriptproject_add_script', args=[self.project.pk])

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def grant_both(self):
        # Registered on the detail route, so the base view asks for change, not add.
        self.grant(ScriptProject, 'view', 'change')
        self.grant(ScriptFile, 'view', 'add')

    @staticmethod
    def upload(name='audit.py', content=SCRIPT + b'# audit\n'):
        return SimpleUploadedFile(name, content, content_type='text/x-python')

    def post(self, **data):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(self.url(), data)

    def test_a_second_script_joins_the_existing_tree(self):
        self.grant_both()
        response = self.post(upload_file=self.upload())
        self.assertHttpStatus(response, 302)

        revision = ScriptProjectRevision.objects.exclude(pk=self.first.pk).get()
        self.assertEqual(sorted(entry['path'] for entry in revision.manifest), ['audit.py', 'deploy.py'])
        # Both are script files, since an uploaded file always is.
        self.assertEqual(
            sorted(entry['source_path'] for entry in revision.script_file_snapshot),
            ['audit.py', 'deploy.py'],
        )
        self.enqueued.assert_called_once()

    def test_the_earlier_declaration_survives(self):
        self.grant_both()
        self.post(upload_file=self.upload())
        self.assertEqual(
            sorted(self.project.script_files.filter(enabled=True).values_list('source_path', flat=True)),
            ['audit.py', 'deploy.py'],
        )

    def test_replacing_a_known_path_needs_the_confirmation(self):
        self.grant_both()
        response = self.post(upload_file=self.upload('deploy.py'))
        self.assertHttpStatus(response, 200)
        self.assertIn('already holds', response.content.decode())
        self.assertEqual(ScriptProjectRevision.objects.count(), 1)
        self.enqueued.assert_not_called()

    def test_the_confirmation_allows_the_replacement_as_a_new_revision(self):
        self.grant_both()
        response = self.post(upload_file=self.upload('deploy.py'), confirm_replace='on')
        self.assertHttpStatus(response, 302)
        revision = ScriptProjectRevision.objects.exclude(pk=self.first.pk).get()
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.assertNotEqual(revision.digest, self.first.digest)
        # The old revision is untouched, so the replaced content stays recoverable.
        self.first.refresh_from_db()
        self.assertEqual([entry['path'] for entry in self.first.manifest], ['deploy.py'])

    def test_two_names_sharing_a_basename_trigger_the_confirmation(self):
        # The whole point of keying on the canonical path: the user picked a different name, but
        # Django flattened it onto one the project already holds.
        self.grant_both()
        response = self.post(upload_file=self.upload('archive/deploy.py'))
        self.assertHttpStatus(response, 200)
        self.assertIn('already holds', response.content.decode())
        self.assertEqual(ScriptProjectRevision.objects.count(), 1)

    def test_a_case_variant_is_refused_on_the_upload_field(self):
        self.grant_both()
        response = self.post(upload_file=self.upload('Deploy.py'))
        self.assertHttpStatus(response, 200)
        self.assertIn('collides', response.content.decode())
        self.assertEqual(ScriptProjectRevision.objects.count(), 1)

    def test_a_non_python_upload_is_refused(self):
        self.grant_both()
        response = self.post(upload_file=self.upload('notes.txt', b'hello\n'))
        self.assertHttpStatus(response, 200)
        self.assertIn('Python source', response.content.decode())

    def test_the_add_permission_is_not_what_this_route_needs(self):
        # ObjectEditView derives the action from the URL, so a detail route asks for change.
        self.grant(ScriptProject, 'view', 'add')
        self.grant(ScriptFile, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_script_file_permission_alone_is_not_enough(self):
        self.grant(ScriptFile, 'view', 'add')
        self.assertHttpStatus(self.client.get(self.url()), 403)

    def test_the_project_permission_alone_is_not_enough(self):
        # The pair the Add Script button renders inert for. Without this the button's decision
        # and the view's could drift apart with nothing catching it.
        self.grant(ScriptProject, 'view', 'change')
        self.assertHttpStatus(self.client.get(self.url()), 403)
