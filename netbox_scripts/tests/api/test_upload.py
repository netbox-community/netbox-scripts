import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from core.models import DataSource, Job, ObjectType
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.jobs import RevisionValidationJob
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage.exceptions import StorageError
from netbox_scripts.tests.plugin_testing import IN_MEMORY_STORAGES
from users.models import ObjectPermission
from utilities.testing import APITestCase

SOURCE = b'from netbox_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class UploadAPITestCase(APITestCase):
    """The projects/<id>/upload/ contract: one Python file, no destination from the request."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(
            name='API Upload Project',
            key='api-upload-project',
            activation_policy=ActivationPolicyChoices.MANUAL,
        )

    def setUp(self):
        super().setUp()
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the runtime tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-upload-api-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))

    def run_validation(self, revision):
        """Drive the queued validation inline, since a TestCase has no worker to pick it up."""
        RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4())).run(
            revision_pk=revision.pk, job_id='x'
        )

    def url(self, project=None):
        return reverse('plugins-api:netbox_scripts-api:scriptproject-upload', args=[(project or self.project).pk])

    def allow_uploads(self):
        # The project's change permission is what the action resolves POST to, and the upload
        # declares a script file, so it creates a Script File as well. The project's add permission
        # is deliberately absent, which is what makes every passing case here also a check that
        # the queryset is narrowed by change: the method-derived narrowing would resolve POST to
        # add, restrict to an empty set, and return 404 instead.
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.add_scriptfile',
        )

    def upload(self, name='deploy.py', content=SOURCE, **extra):
        return self.client.post(
            self.url(), {'file': SimpleUploadedFile(name, content), **extra}, format='multipart', **self.header
        )

    def test_a_python_file_stages_a_revision(self):
        self.allow_uploads()
        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(response.data['id'], revision.pk)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.assertTrue(ScriptFile.objects.filter(project=self.project, source_path='deploy.py').exists())

    def test_a_nested_client_name_lands_at_the_basename(self):
        # No destination comes from the request. A path is client-local structure, so it is
        # flattened here rather than left to whatever the parser happens to do.
        self.allow_uploads()
        response = self.upload(name='automation/nested/deploy.py')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])

    def test_a_repeated_path_needs_confirmation(self):
        self.allow_uploads()
        self.upload()

        response = self.upload(content=SOURCE + b'\n# changed\n')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('deploy.py', str(response.data))

        confirmed = self.upload(content=SOURCE + b'\n# changed\n', confirm_replace=True)
        self.assertEqual(confirmed.status_code, status.HTTP_201_CREATED)

    def test_a_case_variant_is_refused_readably(self):
        self.allow_uploads()
        self.upload()

        response = self.upload(name='Deploy.py')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_file_over_the_size_limit_is_refused_before_it_is_read(self):
        self.allow_uploads()
        limit = 64
        with override_settings(PLUGINS_CONFIG={'netbox_scripts': {'max_file_size': limit}}):
            response = self.upload(content=b'# ' + b'x' * limit + b'\n')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(ScriptProjectRevision.objects.filter(project=self.project).exists())

    def test_a_file_that_is_not_python_is_refused(self):
        self.allow_uploads()
        response = self.upload(name='notes.md', content=b'# notes\n')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_content_the_manifest_refuses_stages_an_invalid_revision(self):
        # The route bounds one file, so a limit that reads the whole tree is not a 400. It stages
        # as an invalid revision instead, which is the case a caller polling the verdict has to
        # expect from a 201.
        self.allow_uploads()
        self.upload()

        with override_settings(PLUGINS_CONFIG={'netbox_scripts': {'max_file_count': 1}}):
            response = self.upload(name='helper.py', content=b'VALUE = 1\n')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        revision = ScriptProjectRevision.objects.get(pk=response.data['id'])
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertIsNone(revision.digest)
        self.assertEqual([error['code'] for error in revision.validation_errors], ['too_many_files'])

    def test_a_data_source_project_is_refused(self):
        self.allow_uploads()
        source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        project = ScriptProject.objects.create(
            name='Synced',
            key='synced',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='automation/netbox',
        )
        response = self.client.post(
            self.url(project), {'file': SimpleUploadedFile('deploy.py', SOURCE)}, format='multipart', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unsafe_branch_route_is_refused_readably(self):
        self.allow_uploads()
        with mock.patch(
            'netbox_scripts.ingestion.branching.require_safe_routing',
            side_effect=ImproperlyConfigured('Script Projects are routed to a branch schema.'),
        ):
            response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('branch schema', str(response.data))
        self.assertFalse(ScriptProjectRevision.objects.filter(project=self.project).exists())

    def test_a_failed_store_is_refused_readably(self):
        self.allow_uploads()
        with mock.patch(
            'netbox_scripts.storage.service.store.write_revision',
            side_effect=StorageError('the backend is unreachable'),
        ):
            response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('could not be stored', str(response.data))

    def test_the_manual_policy_leaves_the_revision_inactive(self):
        self.allow_uploads()
        self.upload()
        revision = ScriptProjectRevision.objects.get(project=self.project)

        self.run_validation(revision)

        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertIsNone(self.project.active_revision_id)

    def test_the_automatic_policy_activates_the_revision(self):
        self.allow_uploads()
        ScriptProject.objects.filter(pk=self.project.pk).update(
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID
        )
        self.upload()
        revision = ScriptProjectRevision.objects.get(project=self.project)

        self.run_validation(revision)

        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_the_project_add_permission_does_not_authorize_an_upload(self):
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.add_scriptproject',
            'netbox_scripts.add_scriptfile',
        )
        response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_script_file_add_permission_is_required_too(self):
        # The upload declares a script file, which is what the UI view requires it for.
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
        )
        response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_child_add_grant_on_another_project_cannot_authorize_this_upload(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        permission = ObjectPermission.objects.create(
            name='Different project', actions=['add'], constraints={'project_id': self.project.pk + 1000}
        )
        permission.object_types.add(ObjectType.objects.get_for_model(ScriptFile))
        permission.users.add(self.user)
        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(self.project.script_files.exists())
        self.assertFalse(self.project.revisions.exists())
        flush.assert_not_called()

    def test_a_successful_upload_emits_one_real_declaration_event(self):
        self.allow_uploads()
        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        events = [event for call in flush.call_args_list for event in call.args[0]]
        declarations = [event for event in events if isinstance(event['object'], ScriptFile)]
        self.assertEqual(len(declarations), 1)
        self.assertEqual(declarations[0]['object'].pk, self.project.script_files.get().pk)

    def test_a_refused_data_source_upload_leaves_no_declaration_or_event(self):
        source = DataSource.objects.create(name='No upload', type='local', source_url='file:///tmp/scripts')
        ScriptProject.objects.filter(pk=self.project.pk).update(
            source_type=ProjectSourceTypeChoices.DATA_SOURCE, data_source=source, data_path='scripts'
        )
        self.allow_uploads()
        with mock.patch('netbox.context_managers.flush_events') as flush:
            response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(self.project.script_files.exists())
        self.assertFalse(self.project.revisions.exists())
        flush.assert_not_called()
