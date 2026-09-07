import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from core.models import DataFile, DataSource, Job
from netbox_scripts import activation
from netbox_scripts.choices import (
    ActivationPolicyChoices,
    FileDiscoveryStatusChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_scripts.ingestion import (
    _data_source_tree,
    check_upload_conflicts,
    current_source_tree,
    ingest_data_source,
    ingest_upload,
    uploaded_source_path,
)
from netbox_scripts.jobs import RevisionValidationJob
from netbox_scripts.models import (
    NetBoxScript,
    ScriptFile,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.storage import service, store
from netbox_scripts.storage.exceptions import StorageError

from .test_branching import routing

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

SCRIPT = b'from netbox_scripts.scripts import Script\n\n\nclass Deploy(Script):\n    pass\n'
DISCOVERABLE_SCRIPT = b"""from netbox_scripts.scripts import Script, StringVar


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


def data_file(source, path, content=b'x'):
    """Add one file to a Data Source's synchronized inventory."""
    # last_updated is editable=False with no auto_now, so a fixture has to set it.
    return DataFile.objects.create(
        source=source,
        path=path,
        size=len(content),
        hash=hashlib.sha256(content).hexdigest(),
        data=content,
        last_updated=timezone.now(),
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
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    def test_the_script_file_is_declared_and_enabled(self):
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        script_file = ScriptFile.objects.get(project=self.project)
        self.assertEqual(script_file.source_path, 'deploy.py')
        self.assertTrue(script_file.enabled)

    def test_the_declaration_exists_before_the_snapshot_is_built(self):
        # The revision freezes the enabled declarations at staging time, so a declaration made
        # afterwards would not be validated. The snapshot naming the path proves the order.
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual([entry['source_path'] for entry in staged.revision.script_file_snapshot], ['deploy.py'])

    def test_the_revision_is_materialized_and_holds_the_content(self):
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertTrue(staged.created)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['deploy.py'])

    def test_validation_is_enqueued_once_for_the_staged_revision(self):
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.enqueued.assert_called_once_with(staged.revision, activate_once=False)

    def test_a_name_needing_canonicalization_is_stored_canonical(self):
        staged = ingest_upload(self.project, filename='./deploy.py', content=SCRIPT)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['deploy.py'])
        self.assertEqual(ScriptFile.objects.get(project=self.project).source_path, 'deploy.py')

    def test_a_refused_name_creates_nothing(self):
        with self.assertRaises(ValidationError):
            ingest_upload(self.project, filename='notes.txt', content=b'hello\n')
        self.assertFalse(ScriptFile.objects.exists())
        self.assertFalse(ScriptProjectRevision.objects.exists())
        self.enqueued.assert_not_called()

    def test_a_data_source_project_refuses_an_upload(self):
        source = DataSource.objects.create(name='Scripts', type='local', source_url='file:///tmp/scripts')
        project = ScriptProject.objects.create(
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
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        # The declaration survives, so the retry stages the same entrypoint configuration.
        self.assertTrue(ScriptFile.objects.filter(project=self.project, enabled=True).exists())
        self.enqueued.assert_not_called()

    def test_re_uploading_a_disabled_path_turns_it_back_on(self):
        # An uploaded file is always an entrypoint, and the row is reused rather than replaced,
        # because Custom Script rows and Job history will reference the declaration.
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        script_file = ScriptFile.objects.get(project=self.project)
        ScriptFile.objects.filter(pk=script_file.pk).update(
            enabled=False, discovery_status=FileDiscoveryStatusChoices.DISCOVERED
        )

        ingest_upload(self.project, filename='deploy.py', content=SCRIPT + b'# changed\n')
        script_file.refresh_from_db()
        self.assertTrue(script_file.enabled)
        self.assertEqual(ScriptFile.objects.filter(project=self.project).count(), 1)

    def test_identical_content_resolves_to_the_existing_revision(self):
        first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        second = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual(first.revision.pk, second.revision.pk)
        self.assertFalse(second.created)

    def test_a_revision_holding_a_verdict_is_not_enqueued_again(self):
        # Re-uploading identical content resolves to the revision that already has the verdict,
        # and only a materialized revision is claimable, so enqueueing it would fail a job over
        # an upload that changed nothing.
        first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        for status in (
            RevisionStatusChoices.VALID,
            RevisionStatusChoices.ACTIVE,
            RevisionStatusChoices.RETIRED,
        ):
            with self.subTest(status=status):
                ScriptProjectRevision.objects.filter(pk=first.revision.pk).update(status=status)
                self.enqueued.reset_mock()

                second = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
                self.assertEqual(second.revision.pk, first.revision.pk)
                self.assertEqual(second.revision.status, status)
                self.enqueued.assert_not_called()

    @override_settings(PLUGINS_CONFIG={'netbox_scripts': {'max_file_size': 8}})
    def test_a_revision_rejected_at_staging_is_not_enqueued(self):
        # A tree the manifest refuses is persisted as an invalid revision that project validation
        # never owned, so it is not claimable either.
        staged = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.INVALID)
        self.enqueued.assert_not_called()

    def test_a_still_materialized_revision_is_enqueued_again(self):
        # The recovery path: a worker that died without recording a verdict leaves the revision
        # claimable, so a repeated upload has to be able to drive it again.
        first = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.enqueued.reset_mock()

        second = ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertEqual(second.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.enqueued.assert_called_once_with(first.revision, activate_once=False)

    def test_a_nested_path_is_preserved_at_this_layer(self):
        # Flattening to a basename happens in Django's uploaded-file handling, so it binds the
        # HTTP form only. This layer keeps whatever path it is handed.
        staged = ingest_upload(self.project, filename='automation/deploy.py', content=SCRIPT)
        self.assertEqual([entry['path'] for entry in staged.revision.manifest], ['automation/deploy.py'])
        self.assertEqual(ScriptFile.objects.get(project=self.project).source_path, 'automation/deploy.py')

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
        self.assertEqual(ScriptFile.objects.filter(project=self.project).count(), 1)

    def test_a_case_variant_basename_is_refused_as_a_sibling_collision(self):
        ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        with self.assertRaises(ValidationError) as ctx:
            ingest_upload(self.project, filename='Deploy.py', content=SCRIPT, base_files={'deploy.py': SCRIPT})
        self.assertIn('collides', str(ctx.exception))
        self.assertEqual(ScriptFile.objects.filter(project=self.project).count(), 1)

    def test_a_second_upload_builds_on_the_un_activated_first(self):
        # A project on the manual policy serves one revision while a newer one waits, and the
        # next upload has to carry that newer tree rather than the one still in force.
        first = ingest_upload(self.project, filename='alpha.py', content=SCRIPT)
        ScriptProjectRevision.objects.filter(pk=first.revision.pk).update(status=RevisionStatusChoices.ACTIVE)
        self.project.active_revision_id = first.revision.pk
        self.project.save(update_fields=('active_revision',))

        ingest_upload(self.project, filename='beta.py', content=SCRIPT, base_files=current_source_tree(self.project))
        staged = ingest_upload(
            self.project, filename='gamma.py', content=SCRIPT, base_files=current_source_tree(self.project)
        )

        self.assertEqual(
            sorted(entry['path'] for entry in staged.revision.manifest),
            ['alpha.py', 'beta.py', 'gamma.py'],
        )

    def test_a_replacement_is_detected_against_the_newest_stored_revision(self):
        # The conflict check and the tree the upload builds on have to read one revision, or
        # the confirmation is skipped for exactly the content it guards.
        first = ingest_upload(self.project, filename='alpha.py', content=SCRIPT)
        ScriptProjectRevision.objects.filter(pk=first.revision.pk).update(status=RevisionStatusChoices.ACTIVE)
        self.project.active_revision_id = first.revision.pk
        self.project.save(update_fields=('active_revision',))
        ingest_upload(self.project, filename='beta.py', content=SCRIPT, base_files=current_source_tree(self.project))

        with self.assertRaises(ValidationError):
            check_upload_conflicts(self.project, 'beta.py', confirm_replace=False)

    def test_an_upload_declares_nothing_when_routing_is_unsafe(self):
        with routing(scriptfile=True), self.assertRaises(ImproperlyConfigured):
            ingest_upload(self.project, filename='deploy.py', content=SCRIPT)
        self.assertFalse(ScriptFile.objects.filter(project=self.project).exists())

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
            sorted(entry['source_path'] for entry in staged.revision.script_file_snapshot),
            ['audit.py', 'deploy.py'],
        )


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class IngestDataSourceTestCase(TestCase):
    """Turning a project's Data Source directory into a revision on its way to a verdict."""

    def setUp(self):
        self.source = DataSource.objects.create(
            name='Scripts Repo', type='local', source_url='file:///tmp/scripts-repo/'
        )
        self.project = ScriptProject.objects.create(
            name='Repo Project',
            key='repo-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='automation/netbox',
        )
        self.enqueued = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    def populate(self):
        """Synchronize a repository holding the project's directory, a sibling, and bytecode."""
        for path in (
            'automation/netbox/deploy.py',
            'automation/netbox/lib/shared.py',
            'automation/netbox/README.md',
            'automation/netbox/__pycache__/deploy.cpython-312.pyc',
            'automation/netbox/lib/stale.pyc',
            'automation/netbox-old/legacy.py',
            'unrelated/other.py',
        ):
            data_file(self.source, path)

    def staged_paths(self, staged):
        return sorted(entry['path'] for entry in staged.revision.manifest)

    def test_the_directory_is_staged_with_its_prefix_stripped(self):
        self.populate()
        staged = ingest_data_source(self.project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(self.staged_paths(staged), ['README.md', 'deploy.py', 'lib/shared.py'])

    def test_a_nested_path_is_preserved(self):
        # Unlike an upload, which Django has already reduced to a basename by the time a form
        # sees it.
        self.populate()
        self.assertIn('lib/shared.py', self.staged_paths(ingest_data_source(self.project)))

    def test_a_file_outside_the_directory_is_excluded(self):
        self.populate()
        paths = self.staged_paths(ingest_data_source(self.project))
        self.assertNotIn('other.py', paths)
        self.assertNotIn('unrelated/other.py', paths)

    def test_a_sibling_directory_sharing_a_prefix_is_excluded(self):
        # Segment-wise, so "automation/netbox" does not claim "automation/netbox-old".
        self.populate()
        self.assertNotIn('legacy.py', self.staged_paths(ingest_data_source(self.project)))

    def test_a_non_python_file_is_stored(self):
        # A directory legitimately holds helper data a script reads. The Python-only rule binds
        # uploads, where every file is an entrypoint.
        self.populate()
        self.assertIn('README.md', self.staged_paths(ingest_data_source(self.project)))

    def test_compiled_artifacts_are_skipped_silently(self):
        self.populate()
        staged = ingest_data_source(self.project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(staged.revision.validation_errors, [])
        self.assertEqual([path for path in self.staged_paths(staged) if path.endswith('.pyc')], [])
        self.assertEqual([path for path in self.staged_paths(staged) if '__pycache__' in path], [])

    def test_any_other_refused_path_invalidates_the_revision(self):
        # Only bytecode is skipped. Everything else the path policy refuses falls through to
        # staging, which records it as an invalid revision naming the path.
        data_file(self.source, 'automation/netbox/{}.py'.format('x' * 300))
        staged = ingest_data_source(self.project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.INVALID)
        self.assertEqual([error['code'] for error in staged.revision.validation_errors], ['path_component_too_long'])
        self.assertEqual(staged.revision.validation_errors[0]['path'], '{}.py'.format('x' * 300))
        self.enqueued.assert_not_called()

    def test_no_script_file_is_declared(self):
        # A new Python file becomes a candidate that has to be selected, never an entrypoint by
        # arrival, which is what makes the selection survive a synchronization.
        self.populate()
        staged = ingest_data_source(self.project)
        self.assertFalse(ScriptFile.objects.filter(project=self.project).exists())
        self.assertEqual(staged.revision.script_file_snapshot, [])

    def test_an_enabled_declaration_is_frozen_into_the_snapshot(self):
        self.populate()
        self.project.select_script_files(['deploy.py'])
        staged = ingest_data_source(self.project)
        self.assertEqual([entry['source_path'] for entry in staged.revision.script_file_snapshot], ['deploy.py'])

    def test_a_declaration_whose_file_is_gone_is_still_frozen_in(self):
        # The verdict has to be able to name the missing path, so the snapshot carries the
        # declaration even though the manifest no longer holds the file.
        self.populate()
        self.project.select_script_files(['deploy.py'])
        DataFile.objects.filter(path='automation/netbox/deploy.py').delete()

        staged = ingest_data_source(self.project)
        self.assertEqual([entry['source_path'] for entry in staged.revision.script_file_snapshot], ['deploy.py'])
        self.assertNotIn('deploy.py', self.staged_paths(staged))

    def test_the_bytes_of_a_file_outside_the_directory_are_never_fetched(self):
        # The scope is decided from paths, so a repository holding other projects does not put
        # their content in this worker's memory.
        self.populate()
        outsider = data_file(self.source, 'unrelated/huge.py', b'z' * 4096)

        with CaptureQueriesContext(connection) as queries:
            tree = _data_source_tree(self.project)

        self.assertNotIn('huge.py', ' '.join(tree))
        fetching = [q['sql'] for q in queries.captured_queries if '"data"' in q['sql']]
        self.assertEqual(len(fetching), 1)
        # The one query that reads bytes is restricted to the in-scope rows by primary key.
        self.assertIn('IN (', fetching[0])
        self.assertNotIn(str(outsider.pk), fetching[0])

    def test_validation_is_enqueued_once_for_the_staged_revision(self):
        self.populate()
        staged = ingest_data_source(self.project)
        self.enqueued.assert_called_once()
        self.assertEqual(self.enqueued.call_args.args, (staged.revision,))
        # The sync path keeps the project policy, so it asks for no one-shot at all.
        self.assertNotIn('activate_once', self.enqueued.call_args.kwargs)

    def test_a_synchronization_that_changed_nothing_is_a_no_op(self):
        # Identical content under an unchanged entrypoint configuration resolves to the revision
        # that already holds a verdict, and only a materialized revision is claimable, so
        # enqueueing it again would fail a job over a synchronization that changed nothing.
        self.populate()
        first = ingest_data_source(self.project)
        ScriptProjectRevision.objects.filter(pk=first.revision.pk).update(status=RevisionStatusChoices.VALID)
        self.enqueued.reset_mock()

        second = ingest_data_source(self.project)
        self.assertEqual(second.revision.pk, first.revision.pk)
        self.assertFalse(second.created)
        self.assertEqual(ScriptProjectRevision.objects.filter(project=self.project).count(), 1)
        self.enqueued.assert_not_called()

    def test_an_empty_directory_stages_an_empty_revision(self):
        # Staged faithfully rather than specially refused. A project with no declarations reaches
        # a vacuously valid revision that publishes nothing, and one with declarations reaches an
        # invalid verdict naming every missing path.
        staged = ingest_data_source(self.project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(staged.revision.manifest, [])

    def test_an_upload_project_is_refused(self):
        upload = ScriptProject.objects.create(name='Uploaded', key='uploaded')
        with self.assertRaises(ValidationError) as ctx:
            ingest_data_source(upload)
        self.assertIn('uploaded', str(ctx.exception))
        self.assertFalse(ScriptProjectRevision.objects.filter(project=upload).exists())
        self.enqueued.assert_not_called()


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ValidationJobActivationTestCase(TestCase):
    """The activation policy decides whether a valid revision goes live inside the job."""

    def setUp(self):
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')

    def valid_revision(self, policy):
        ScriptProject.objects.filter(pk=self.project.pk).update(activation_policy=policy)
        self.project.refresh_from_db()
        revision = service.stage_revision(self.project, {'deploy.py': SCRIPT}).revision
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        revision.refresh_from_db()
        return revision

    def run_job(self, revision, **kwargs):
        """Drive the job body past validation, with the verdict already recorded."""
        # Bound to a real Job row, because the runner's logger writes to it.
        runner = RevisionValidationJob(Job.objects.create(name='validation-test', job_id=uuid.uuid4()))
        with mock.patch('netbox_scripts.jobs.validate_revision', return_value=revision):
            runner.run(revision_pk=revision.pk, job_id='x', **kwargs)

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

    def test_a_one_shot_activates_under_a_manual_policy(self):
        # What the upload form's "Activate this upload" box asks for, without touching the policy
        # every later revision is judged by.
        revision = self.valid_revision(ActivationPolicyChoices.MANUAL)
        self.run_job(revision, activate_once=True)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertEqual(self.project.activation_policy, ActivationPolicyChoices.MANUAL)

    def test_a_one_shot_does_not_activate_an_invalid_revision(self):
        revision = self.valid_revision(ActivationPolicyChoices.MANUAL)
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.INVALID, validation_errors=[{'path': 'deploy.py', 'message': 'bad'}]
        )
        revision.refresh_from_db()
        self.run_job(revision, activate_once=True)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_an_invalid_revision_is_never_activated(self):
        revision = self.valid_revision(ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.INVALID, validation_errors=[{'path': 'deploy.py', 'message': 'bad'}]
        )
        revision.refresh_from_db()
        self.run_job(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)


@override_settings(
    STORAGES=IN_MEMORY_STORAGES,
    PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': None}},
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
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))

    def run_validation(self, revision):
        runner = RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4()))
        runner.run(revision_pk=revision.pk, job_id='x')

    def test_an_uploaded_script_reaches_active_and_discovered(self):
        project = ScriptProject.objects.create(
            name='Deploy Devices',
            key='deploy-devices',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        staged = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(
            ScriptFile.objects.get(project=project).discovery_status,
            FileDiscoveryStatusChoices.PENDING,
        )

        self.run_validation(staged.revision)

        staged.revision.refresh_from_db()
        project.refresh_from_db()
        script_file = ScriptFile.objects.get(project=project)
        self.assertEqual(staged.revision.validation_errors, [])
        self.assertEqual(staged.revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(project.active_revision_id, staged.revision.pk)
        self.assertEqual(script_file.discovery_status, FileDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(script_file.discovery_error, '')

        # The point of the whole slice: the uploaded class is a first-class object now.
        script = NetBoxScript.objects.get(project=project)
        self.assertEqual(script.class_name, 'HelloWorld')
        self.assertEqual(script.display_name, 'Hello World')
        self.assertEqual(script.description, 'Smoke test for the upload path')
        self.assertEqual(script.last_seen_revision_id, staged.revision.pk)
        self.assertTrue(script.is_executable)

    def test_a_replacement_that_drops_a_class_retires_it(self):
        project = ScriptProject.objects.create(
            name='Two Scripts',
            key='two-scripts',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        first = ingest_upload(project, filename='hello_world.py', content=TWO_SCRIPTS)
        self.run_validation(first.revision)
        self.assertEqual(NetBoxScript.objects.filter(project=project).count(), 2)

        second = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.run_validation(second.revision)

        self.assertTrue(NetBoxScript.objects.get(project=project, class_name='Farewell').is_retired)
        self.assertFalse(NetBoxScript.objects.get(project=project, class_name='HelloWorld').is_retired)

    def test_a_retired_script_returns_when_its_revision_is_activated_again(self):
        # Reuse of the row rather than a replacement is what preserves the Job history. Driven
        # through activation rather than a third upload, because re-uploading content the project
        # has held before resolves to the existing revision, which validation can no longer
        # claim. That gap is ingestion's, not activation's.
        project = ScriptProject.objects.create(
            name='Returning',
            key='returning',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        first = ingest_upload(project, filename='hello_world.py', content=TWO_SCRIPTS)
        self.run_validation(first.revision)
        original_pk = NetBoxScript.objects.get(project=project, class_name='Farewell').pk

        dropped = ingest_upload(project, filename='hello_world.py', content=DISCOVERABLE_SCRIPT)
        self.run_validation(dropped.revision)
        self.assertTrue(NetBoxScript.objects.get(project=project, class_name='Farewell').is_retired)

        first.revision.refresh_from_db()
        activation.activate_revision(first.revision)
        row = NetBoxScript.objects.get(project=project, class_name='Farewell')
        self.assertEqual(row.pk, original_pk)
        self.assertFalse(row.is_retired)

    def test_a_manual_project_stops_at_valid_and_still_discovers(self):
        project = ScriptProject.objects.create(
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
            ScriptFile.objects.get(project=project).discovery_status,
            FileDiscoveryStatusChoices.DISCOVERED,
        )

    def test_a_script_that_cannot_import_is_an_invalid_verdict(self):
        project = ScriptProject.objects.create(name='Broken', key='broken')
        staged = ingest_upload(project, filename='broken.py', content=b'this is not python(\n')
        self.run_validation(staged.revision)

        staged.revision.refresh_from_db()
        script_file = ScriptFile.objects.get(project=project)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.INVALID)
        self.assertTrue(staged.revision.validation_errors)
        self.assertEqual(script_file.discovery_status, FileDiscoveryStatusChoices.FAILED)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class DataSourceToActiveTestCase(TestCase):
    """
    A Data Source-backed project through real validation, one synchronization at a time.

    The entrypoint selection survives a synchronization, and a selected entrypoint that
    disappears from the source invalidates the new revision while the project keeps serving
    the one it already had.
    """

    def setUp(self):
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-sync-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))
        # Enqueueing hands the task to a real queue, and this test drives the job itself.
        self.enterContext(mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None))

        self.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        self.project = ScriptProject.objects.create(
            name='Repo Project',
            key='repo-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='scripts',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )

    def reconcile(self):
        """Reconcile the project and drive the revision it staged to a verdict, as a sync does."""
        staged = ingest_data_source(self.project)
        if staged.revision.status == RevisionStatusChoices.MATERIALIZED:
            runner = RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4()))
            runner.run(revision_pk=staged.revision.pk, job_id='x')
        staged.revision.refresh_from_db()
        # Re-read rather than refresh, because current_revision is cached per instance.
        self.project = ScriptProject.objects.get(pk=self.project.pk)
        return staged.revision

    def test_the_selection_survives_and_a_vanished_script_file_spares_the_active_revision(self):
        data_file(self.source, 'scripts/hello_world.py', DISCOVERABLE_SCRIPT)
        # Nothing is declared yet, so the first synchronization publishes nothing.
        self.reconcile()
        self.assertFalse(NetBoxScript.objects.filter(project=self.project).exists())

        self.project.select_script_files(['hello_world.py'])
        first = self.reconcile()
        self.assertEqual(first.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, first.pk)
        script = NetBoxScript.objects.get(project=self.project)
        self.assertTrue(script.is_executable)

        # A Python file added to the source is a candidate, so the selection is unchanged and
        # nothing new publishes until someone selects it.
        data_file(self.source, 'scripts/audit.py', SCRIPT)
        active = self.reconcile()
        self.assertEqual(active.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual([entry['source_path'] for entry in active.script_file_snapshot], ['hello_world.py'])
        self.assertEqual(NetBoxScript.objects.filter(project=self.project, is_retired=False).count(), 1)

        # The selected file is deleted from the source.
        DataFile.objects.filter(path='scripts/hello_world.py').delete()
        vanished = self.reconcile()

        self.assertEqual(vanished.status, RevisionStatusChoices.INVALID)
        self.assertIn('hello_world.py', str(vanished.validation_errors))
        self.assertEqual(self.project.active_revision_id, active.pk)
        script.refresh_from_db()
        self.assertTrue(script.is_executable)
