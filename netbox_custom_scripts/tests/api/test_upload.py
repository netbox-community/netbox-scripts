import shutil
import tempfile
import uuid
from pathlib import Path

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from core.models import DataSource, Job
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.jobs import RevisionValidationJob
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from utilities.testing import APITestCase

SOURCE = b'from netbox_custom_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class UploadAPITestCase(APITestCase):
    """The projects/<id>/upload/ contract: one Python file, no destination from the request."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(
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
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': str(root)}})
        )

    def run_validation(self, revision):
        """Drive the queued validation inline, since a TestCase has no worker to pick it up."""
        RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4())).run(
            revision_pk=revision.pk, job_id='x'
        )

    def url(self, project=None):
        return reverse(
            'plugins-api:netbox_custom_scripts-api:customscriptproject-upload', args=[(project or self.project).pk]
        )

    def allow_uploads(self):
        # The project's change permission is what the action resolves POST to, and the upload
        # declares an entrypoint, so it creates a Module as well. The project's add permission
        # is deliberately absent, which is what makes every passing case here also a check that
        # the queryset is narrowed by change: the method-derived narrowing would resolve POST to
        # add, restrict to an empty set, and return 404 instead.
        self.add_permissions(
            'netbox_custom_scripts.view_customscriptproject',
            'netbox_custom_scripts.change_customscriptproject',
            'netbox_custom_scripts.add_customscriptmodule',
        )

    def upload(self, name='deploy.py', content=SOURCE, **extra):
        return self.client.post(
            self.url(), {'file': SimpleUploadedFile(name, content), **extra}, format='multipart', **self.header
        )

    def test_a_python_file_stages_a_revision(self):
        self.allow_uploads()
        response = self.upload()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        revision = CustomScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(response.data['id'], revision.pk)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.assertTrue(CustomScriptModule.objects.filter(project=self.project, source_path='deploy.py').exists())

    def test_a_nested_client_name_lands_at_the_basename(self):
        # No destination comes from the request. A path is client-local structure, so it is
        # flattened here rather than left to whatever the parser happens to do.
        self.allow_uploads()
        response = self.upload(name='automation/nested/deploy.py')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        revision = CustomScriptProjectRevision.objects.get(project=self.project)
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

    def test_a_file_that_is_not_python_is_refused(self):
        self.allow_uploads()
        response = self.upload(name='notes.md', content=b'# notes\n')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_data_source_project_is_refused(self):
        self.allow_uploads()
        source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        project = CustomScriptProject.objects.create(
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

    def test_the_manual_policy_leaves_the_revision_inactive(self):
        self.allow_uploads()
        self.upload()
        revision = CustomScriptProjectRevision.objects.get(project=self.project)

        self.run_validation(revision)

        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertIsNone(self.project.active_revision_id)

    def test_the_automatic_policy_activates_the_revision(self):
        self.allow_uploads()
        CustomScriptProject.objects.filter(pk=self.project.pk).update(
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID
        )
        self.upload()
        revision = CustomScriptProjectRevision.objects.get(project=self.project)

        self.run_validation(revision)

        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_the_project_add_permission_does_not_authorize_an_upload(self):
        self.add_permissions(
            'netbox_custom_scripts.view_customscriptproject',
            'netbox_custom_scripts.add_customscriptproject',
            'netbox_custom_scripts.add_customscriptmodule',
        )
        response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_module_add_permission_is_required_too(self):
        # The upload declares an entrypoint, which is what the UI view requires it for.
        self.add_permissions(
            'netbox_custom_scripts.view_customscriptproject',
            'netbox_custom_scripts.change_customscriptproject',
        )
        response = self.upload()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
