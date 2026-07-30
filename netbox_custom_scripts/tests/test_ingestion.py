import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from core.models import DataSource, Job
from netbox_custom_scripts import activation
from netbox_custom_scripts.choices import (
    ActivationPolicyChoices,
    ModuleDiscoveryStatusChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_custom_scripts.ingestion import ingest_upload, uploaded_source_path
from netbox_custom_scripts.jobs import RevisionValidationJob
from netbox_custom_scripts.models import (
    CustomScript,
    CustomScriptModule,
    CustomScriptProject,
    CustomScriptProjectRevision,
)
from netbox_custom_scripts.storage import service, store
from netbox_custom_scripts.storage.exceptions import StorageError

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_custom_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

SCRIPT = b'from netbox_custom_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'
DISCOVERABLE_SCRIPT = b"""from netbox_custom_scripts.scripts import Script, StringVar


class HelloWorld(Script):
    class Meta:
        name = 'Hello World'
        description = 'Smoke test for the upload path'

    greeting = StringVar(max_length=64, default='hello')

    def run(self, data, commit):
        return data['greeting']
"""

TWO_SCRIPTS = (
    DISCOVERABLE_SCRIPT
    + b"""

class Farewell(Script):
    class Meta:
        name = 'Farewell'

    def run(self, data, commit):
        return 'bye'
"""
)


class UploadedSourcePathTestCase(TestCase):
    """The rule turning a browser-supplied file name into a stored source path."""

    def test_a_plain_name_is_kept(self):
        self.assertEqual(uploaded_source_path('deploy.py'), 'deploy.py')

    def test_a_name_is_canonicalized(self):
        self.assertEqual(uploaded_source_path('./deploy.py'), 'deploy.py')

    def test_a_nested_name_keeps_its_directories(self):
        self.assertEqual(uploaded_source_path('automation/deploy.py'), 'automation/deploy.py')

    def test_a_non_python_name_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            uploaded_source_path('notes.txt')
        self.assertIn('Python source', str(ctx.exception))

    def test_a_traversing_name_is_refused(self):
        with self.assertRaises(ValidationError):
            uploaded_source_path('../escape.py')

    def test_an_absolute_name_is_refused(self):
        with self.assertRaises(ValidationError):
            uploaded_source_path('/etc/deploy.py')

    def test_a_compiled_artifact_is_refused(self):
        with self.assertRaises(ValidationError):
            uploaded_source_path('deploy.pyc')


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class IngestUploadTestCase(TestCase):
    """Ordering and failure handling of the one source-ingestion entry point."""

    def setUp(self):
        self.project = CustomScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    def test_the_entrypoint_is_declared_and_enabled(self):
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        module = CustomScriptModule.objects.get(project=self.project)
        self.assertEqual(module.source_path, 'deploy.py')
        self.assertTrue(module.enabled)

    def test_the_declaration_exists_before_the_snapshot_is_built(self):
        # The revision freezes the enabled declarations at staging time, so a declaration made
        # afterwards would not be validated. The snapshot naming the path proves the order.
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual([entry['source_path'] for entry in staged.revision.entrypoint_snapshot], ['deploy.py'])

    def test_the_revision_is_materialized_and_holds_the_content(self):
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertTrue(staged.created)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['deploy.py'])

    def test_validation_is_enqueued_once_for_the_staged_revision(self):
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.enqueued.assert_called_once_with(staged.revision)

    def test_a_name_needing_canonicalization_is_stored_canonical(self):
        staged = ingest_upload(self.project, filename='./deploy.py', content=SCRIPT)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['deploy.py'])
        self.assertEqual(CustomScriptModule.objects.get(project=self.project).source_path, 'deploy.py')

    def test_a_refused_name_creates_nothing(self):
        with self.assertRaises(ValidationError):
            ingest_upload(self.project, filename='notes.txt', content=b'hello\n')
        self.assertFalse(CustomScriptModule.objects.exists())
        self.assertFalse(CustomScriptProjectRevision.objects.exists())
        self.enqueued.assert_not_called()

    def test_a_data_source_project_refuses_an_upload(self):
        source = DataSource.objects.create(name='Scripts', type='local', source_url='file:///tmp/scripts')
        project = CustomScriptProject.objects.create(
            name='Synced',
            key='synced',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='scripts',
        )
        with self.assertRaises(ValidationError) as ctx:
            ingest_upload(project, filename='deploy.py', content=SCRIPT)
        self.assertIn('Data Source', str(ctx.exception))

    def test_a_storage_failure_leaves_a_retryable_revision(self):
        with (
            mock.patch.object(store, 'write_revision', side_effect=StorageError('the backend refused')),
            self.assertRaises(StorageError),
        ):
            ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        revision = CustomScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        # The declaration survives, so the retry stages the same entrypoint configuration.
        self.assertTrue(CustomScriptModule.objects.filter(project=self.project, enabled=True).exists())
        self.enqueued.assert_not_called()

    def test_re_uploading_a_disabled_path_turns_it_back_on(self):
        # An uploaded file is always an entrypoint, and the row is reused rather than replaced,
        # because Custom Script rows and Job history will reference the declaration.
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        module = CustomScriptModule.objects.get(project=self.project)
        CustomScriptModule.objects.filter(pk=module.pk).update(
            enabled=False, discovery_status=ModuleDiscoveryStatusChoices.DISCOVERED
        )

        ingest_upload(self.project, filename='deploy.py', content=SCRIPT + b'# changed\n')
        module.refresh_from_db()
        self.assertTrue(module.enabled)
        self.assertEqual(CustomScriptModule.objects.filter(project=self.project).count(), 1)

    def test_identical_content_resolves_to_the_existing_revision(self):
        first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        second = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual(first.revision.pk, second.revision.pk)
        self.assertFalse(second.created)

    def test_a_nested_path_is_preserved_at_this_layer(self):
        # Flattening to a basename happens in Django's uploaded-file handling, so it binds the
        # HTTP upload only. Data Source reconciliation reaches this function with real directory
        # paths and needs them kept.
        staged = ingest_upload(self.project, filename='automation/deploy.py', content=SCRIPT)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['automation/deploy.py'])
        self.assertEqual(CustomScriptModule.objects.get(project=self.project).source_path, 'automation/deploy.py')

    def test_a_repeated_path_replaces_its_content_under_one_declaration(self):
        # Two uploads whose names share a basename both arrive here as that basename, so this is
        # the shape a user hits by uploading "automation/deploy.py" then "audit/deploy.py". The
        # content is replaced and the declaration is reused, which is why the second upload has
        # to ask first, keyed on the canonical path rather than the name the user picked.
        first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        other = SCRIPT + b'# a different file entirely\n'
        second = ingest_upload(self.project, filename='deploy.py', content=other, base_files={'deploy.py': SCRIPT})

        self.assertEqual([entry['path'] for entry in second.revision.manifest], ['deploy.py'])
        self.assertNotEqual(second.revision.digest, first.revision.digest)
        self.assertEqual(CustomScriptModule.objects.filter(project=self.project).count(), 1)

    def test_a_case_variant_basename_is_refused_as_a_sibling_collision(self):
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        with self.assertRaises(ValidationError) as ctx:
            ingest_upload(self.project, filename='Deploy.py', content=SCRIPT, base_files={'deploy.py': SCRIPT})
        self.assertIn('collides', str(ctx.exception))
        self.assertEqual(CustomScriptModule.objects.filter(project=self.project).count(), 1)

    def test_base_files_are_carried_into_the_new_revision(self):
        base = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        existing = {'deploy.py': SCRIPT}
        staged = ingest_upload(self.project, filename='audit.py', content=SCRIPT, base_files=existing)
        self.assertNotEqual(staged.revision.pk, base.revision.pk)
        self.assertEqual(
            sorted(entry['path'] for entry in staged.revision.manifest),
            ['audit.py', 'deploy.py'],
        )
        self.assertEqual(
            sorted(entry['source_path'] for entry in staged.revision.entrypoint_snapshot),
            ['audit.py', 'deploy.py'],
        )


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ValidationJobActivationTestCase(TestCase):
    """The activation policy decides whether a valid revision goes live inside the job."""

    def setUp(self):
        self.project = CustomScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')

    def valid_revision(self, policy):
        CustomScriptProject.objects.filter(pk=self.project.pk).update(activation_policy=policy)
        self.project.refresh_from_db()
        revision = service.stage_revision(self.project, {'deploy.py': SCRIPT}).revision
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        revision.refresh_from_db()
        return revision

    def run_job(self, revision):
        """Drive the job body past validation, with the verdict already recorded."""
        # Bound to a real Job row, because the runner's logger writes to it.
        runner = RevisionValidationJob(Job.objects.create(name='validation-test', job_id=uuid.uuid4()))
        with mock.patch('netbox_custom_scripts.jobs.validate_revision', return_value=revision):
            runner.run(revision_pk=revision.pk, job_id='x')

    def test_an_automatic_project_activates_the_valid_revision(self):
        revision = self.valid_revision(ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        self.run_job(revision)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_a_manual_project_leaves_the_revision_valid(self):
        revision = self.valid_revision(ActivationPolicyChoices.MANUAL)
        self.run_job(revision)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertIsNone(self.project.active_revision_id)

    def test_an_invalid_revision_is_never_activated(self):
        revision = self.valid_revision(ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.INVALID, validation_errors=[{'path': 'deploy.py', 'message': 'bad'}]
        )
        revision.refresh_from_db()
        self.run_job(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': None}},
)
class UploadToActiveTestCase(TestCase):
    """
    The whole slice in one pass: upload, stage, validate, discover, activate.

    Exercises the real validation job rather than a stub, so the runtime cache, the private
    package loader, and Script discovery all take part. This is what proves an uploaded file
    actually reaches the state a user is waiting for.
    """

    def setUp(self):
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-slice-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': str(root)}})
        )

    def run_validation(self, revision):
        runner = RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4()))
        runner.run(revision_pk=revision.pk, job_id='x')

    def test_an_uploaded_script_reaches_active_and_discovered(self):
        project = CustomScriptProject.objects.create(
            name='Deploy Devices',
            key='deploy-devices',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        staged = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(
            CustomScriptModule.objects.get(project=project).discovery_status,
            ModuleDiscoveryStatusChoices.PENDING,
        )

        self.run_validation(staged.revision)

        staged.revision.refresh_from_db()
        project.refresh_from_db()
        module = CustomScriptModule.objects.get(project=project)
        self.assertEqual(staged.revision.validation_errors, [])
        self.assertEqual(staged.revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(project.active_revision_id, staged.revision.pk)
        self.assertEqual(module.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(module.discovery_error, '')

        # The point of the whole slice: the uploaded class is a first-class object now.
        script = CustomScript.objects.get(project=project)
        self.assertEqual(script.class_name, 'HelloWorld')
        self.assertEqual(script.display_name, 'Hello World')
        self.assertEqual(script.description, 'Smoke test for the upload path')
        self.assertEqual(script.last_seen_revision_id, staged.revision.pk)
        self.assertTrue(script.is_executable)

    def test_a_replacement_that_drops_a_class_retires_it(self):
        project = CustomScriptProject.objects.create(
            name='Two Scripts',
            key='two-scripts',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        first = ingest_upload(project, filename='hello_world.py', content=TWO_SCRIPTS)
        self.run_validation(first.revision)
        self.assertEqual(CustomScript.objects.filter(project=project).count(), 2)

        second = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.run_validation(second.revision)

        self.assertTrue(CustomScript.objects.get(project=project, class_name='Farewell').is_retired)
        self.assertFalse(CustomScript.objects.get(project=project, class_name='HelloWorld').is_retired)

    def test_a_retired_script_returns_when_its_revision_is_activated_again(self):
        # Reuse of the row rather than a replacement is what preserves the Job history. Driven
        # through activation rather than a third upload, because re-uploading content the project
        # has held before resolves to the existing revision, which validation can no longer
        # claim. That gap is ingestion's, not activation's.
        project = CustomScriptProject.objects.create(
            name='Returning',
            key='returning',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        first = ingest_upload(project, filename='hello_world.py', content=TWO_SCRIPTS)
        self.run_validation(first.revision)
        original_pk = CustomScript.objects.get(project=project, class_name='Farewell').pk

        dropped = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.run_validation(dropped.revision)
        self.assertTrue(CustomScript.objects.get(project=project, class_name='Farewell').is_retired)

        first.revision.refresh_from_db()
        activation.activate_revision(first.revision)
        row = CustomScript.objects.get(project=project, class_name='Farewell')
        self.assertEqual(row.pk, original_pk)
        self.assertFalse(row.is_retired)

    def test_a_manual_project_stops_at_valid_and_still_discovers(self):
        project = CustomScriptProject.objects.create(
            name='Manual Devices', key='manual-devices', activation_policy=ActivationPolicyChoices.MANUAL
        )
        staged = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.run_validation(staged.revision)

        staged.revision.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(staged.revision.status, RevisionStatusChoices.VALID)
        self.assertIsNone(project.active_revision_id)
        # Discovery is a property of validation, not of activation.
        self.assertEqual(
            CustomScriptModule.objects.get(project=project).discovery_status,
            ModuleDiscoveryStatusChoices.DISCOVERED,
        )

    def test_a_script_that_cannot_import_is_an_invalid_verdict(self):
        project = CustomScriptProject.objects.create(name='Broken', key='broken')
        staged = ingest_upload(project, filename='broken.py', content=b'this is not python(\n')
        self.run_validation(staged.revision)

        staged.revision.refresh_from_db()
        module = CustomScriptModule.objects.get(project=project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.INVALID)
        self.assertTrue(staged.revision.validation_errors)
        self.assertEqual(module.discovery_status, ModuleDiscoveryStatusChoices.FAILED)
