import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import DataFile, DataSource, Job
from core.signals import post_sync
from netbox_scripts import jobs
from netbox_scripts.choices import (
    ActivationPolicyChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_scripts.jobs import ProjectReconciliationJob, RevisionValidationJob
from netbox_scripts.models import (
    CustomScript,
    CustomScriptModule,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.storage import service, store
from netbox_scripts.storage.exceptions import StorageError
from netbox_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_scripts.tests.test_ingestion import SCRIPT, data_file

SIGNAL_LOGGER = 'netbox.plugins.netbox_scripts.storage'


def data_source_project(source, key, data_path, **kwargs):
    return ScriptProject.objects.create(
        name=key.replace('-', ' ').title(),
        key=key,
        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        data_source=source,
        data_path=data_path,
        **kwargs,
    )


class ReconciliationSignalTestCase(TestCase):
    """Which projects a completed synchronization reconciles, and how the receiver fails."""

    @classmethod
    def setUpTestData(cls):
        cls.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        cls.other = DataSource.objects.create(name='Other Repo', type='local', source_url='file:///tmp/other/')
        data_source_project(cls.source, 'repo-project', 'scripts')
        data_source_project(cls.source, 'tools-project', 'tools')
        data_source_project(cls.other, 'elsewhere-project', 'scripts')
        ScriptProject.objects.create(name='Uploaded', key='uploaded')

    def setUp(self):
        self.enqueued = self.enterContext(
            mock.patch.object(ProjectReconciliationJob, 'enqueue_reconciliation', return_value=None)
        )

    def reconciled(self):
        return sorted(call.args[0].key for call in self.enqueued.call_args_list)

    def test_one_job_is_enqueued_for_each_project_on_that_source(self):
        # One Job per project rather than one per Data Source, because each project has its own
        # lock, revision chain and activation policy.
        post_sync.send(sender=DataSource, instance=self.source)
        self.assertEqual(self.reconciled(), ['repo-project', 'tools-project'])

    def test_a_project_on_another_source_is_left_alone(self):
        post_sync.send(sender=DataSource, instance=self.other)
        self.assertEqual(self.reconciled(), ['elsewhere-project'])

    def test_an_upload_project_is_never_reconciled(self):
        post_sync.send(sender=DataSource, instance=self.source)
        self.assertNotIn('uploaded', self.reconciled())

    def test_a_source_no_project_uses_enqueues_nothing(self):
        unused = DataSource.objects.create(name='Unused', type='local', source_url='file:///tmp/unused/')
        post_sync.send(sender=DataSource, instance=unused)
        self.enqueued.assert_not_called()

    def test_a_failure_to_enqueue_never_fails_the_synchronization(self):
        # Core sends post_sync with send() rather than send_robust(), as the last statement of
        # DataSource.sync(), so an exception escaping here fails the operator's own job.
        self.enqueued.side_effect = RuntimeError('the queue is unreachable')
        with self.assertLogs(SIGNAL_LOGGER, level='ERROR') as captured:
            post_sync.send(sender=DataSource, instance=self.source)
        self.assertIn('the queue is unreachable', '\n'.join(captured.output))


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ReconciliationEnqueueTestCase(TestCase):
    """What the receiver leaves behind for a worker to pick up."""

    def setUp(self):
        self.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        self.project = data_source_project(self.source, 'repo-project', 'scripts')
        data_file(self.source, 'scripts/deploy.py', SCRIPT)

    def test_the_job_row_carries_the_project(self):
        # The pk travels in the payload, because Job.clean() refuses an instance link for a model
        # without the jobs feature.
        with self.captureOnCommitCallbacks():
            post_sync.send(sender=DataSource, instance=self.source)
        job = Job.objects.get(name=ProjectReconciliationJob.name)
        self.assertEqual(job.data, {'project_id': self.project.pk})

    def test_the_receiver_does_no_storage_work_of_its_own(self):
        # The committing process performs no storage I/O, which is the whole reason
        # reconciliation is a Job.
        with mock.patch.object(service, 'stage_revision') as staged, self.captureOnCommitCallbacks():
            post_sync.send(sender=DataSource, instance=self.source)
        staged.assert_not_called()
        self.assertTrue(Job.objects.filter(name=ProjectReconciliationJob.name).exists())
        self.assertFalse(ScriptProjectRevision.objects.exists())


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ProjectReconciliationJobTestCase(TestCase):
    """The Job body: it stages the directory as it stands when the worker runs it."""

    def setUp(self):
        self.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        self.project = data_source_project(self.source, 'repo-project', 'scripts')
        data_file(self.source, 'scripts/deploy.py', SCRIPT)
        self.validated = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )

    def run_job(self, project_id=None):
        job = ProjectReconciliationJob.enqueue(
            immediate=True, project_id=self.project.pk if project_id is None else project_id
        )
        job.refresh_from_db()
        return job

    def test_the_directory_is_staged_and_handed_to_validation(self):
        job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['deploy.py'])
        self.validated.assert_called_once_with(revision)

    def test_a_file_added_after_the_enqueue_is_included(self):
        # The inventory is read when the job runs, not when it was enqueued.
        data_file(self.source, 'scripts/audit.py', SCRIPT)
        self.run_job()
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(sorted(entry['path'] for entry in revision.manifest), ['audit.py', 'deploy.py'])

    def test_a_second_job_over_an_unchanged_directory_does_nothing(self):
        # Two synchronizations in quick succession enqueue two jobs. The second resolves to the
        # revision the first created, so a duplicate is a cheap no-op rather than a problem.
        self.run_job()
        revision = ScriptProjectRevision.objects.get(project=self.project)
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        self.validated.reset_mock()

        job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(ScriptProjectRevision.objects.filter(project=self.project).count(), 1)
        self.validated.assert_not_called()
        # This project activates manually and has never activated anything, so the revision the
        # second job resolves to is the one an operator has yet to act on.
        self.assertIn('waiting for an operator', str(job.log_entries))

    def test_a_project_deleted_before_the_job_runs_completes_with_nothing_to_do(self):
        deleted_pk = self.project.pk
        self.project.delete()
        job = self.run_job(project_id=deleted_pk)
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertIn('no longer exists', str(job.log_entries))

    def test_unsafe_routing_fails_the_job_before_any_work(self):
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        runner = ProjectReconciliationJob(Job.objects.create(name='reconcile-test', job_id=uuid.uuid4()))
        with (
            mock.patch.object(jobs.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertRaises(JobFailed),
        ):
            runner.run(project_id=self.project.pk, job_id='later')
        self.assertIn('Routing is unsafe.', str(runner.job.log_entries))
        self.assertFalse(ScriptProjectRevision.objects.exists())

    def test_a_storage_failure_fails_the_job_and_leaves_a_retryable_revision(self):
        with mock.patch.object(store, 'write_revision', side_effect=StorageError('the backend refused')):
            job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertIn('needs another run', str(job.log_entries))
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        self.validated.assert_not_called()

    def test_a_directory_the_path_policy_refuses_fails_no_job(self):
        # A content problem is a verdict on the revision, not a broken worker, so the job that
        # records it succeeds.
        data_file(self.source, 'scripts/{}.py'.format('x' * 300), SCRIPT)
        job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        revision = ScriptProjectRevision.objects.get(project=self.project)
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertIn('problem(s) recorded', str(job.log_entries))
        self.validated.assert_not_called()


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ReconciliationPolicyTestCase(TestCase):
    """The chain a synchronization sets off, and what the activation policy does with it."""

    def setUp(self):
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-reconcile-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))
        # A worker would pick the validation up. Its queue handoff never fires under TestCase, so
        # it runs inline here and the whole chain stays inside one test.
        self.enterContext(
            mock.patch.object(
                RevisionValidationJob,
                'enqueue_validation',
                side_effect=lambda revision, **kwargs: RevisionValidationJob.enqueue(
                    immediate=True, revision_pk=revision.pk
                ),
            )
        )
        self.source = DataSource.objects.create(name='Scripts Repo', type='local', source_url='file:///tmp/repo/')
        self.project = data_source_project(
            self.source,
            'repo-project',
            'scripts',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        CustomScriptModule.objects.create(project=self.project, source_path='deploy.py', enabled=True)
        data_file(self.source, 'scripts/deploy.py', SCRIPT)

    def run_reconciliation(self):
        """Run one reconciliation Job to completion and return it."""
        job = ProjectReconciliationJob.enqueue(immediate=True, project_id=self.project.pk)
        job.refresh_from_db()
        # Re-read rather than refresh, because current_revision is cached per instance.
        self.project = ScriptProject.objects.get(pk=self.project.pk)
        return job

    def reconcile(self):
        """Run one reconciliation and return the newest revision it left behind."""
        self.assertEqual(self.run_reconciliation().status, JobStatusChoices.STATUS_COMPLETED)
        return self.project.latest_revision()

    def test_an_automatic_project_ends_up_serving_the_new_revision(self):
        revision = self.reconcile()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertTrue(CustomScript.objects.filter(project=self.project, class_name='Deploy').exists())

    def test_a_manual_project_reaches_valid_and_keeps_serving_what_it_had(self):
        active = self.reconcile()
        ScriptProject.objects.filter(pk=self.project.pk).update(activation_policy=ActivationPolicyChoices.MANUAL)
        data_file(self.source, 'scripts/audit.py', SCRIPT)

        revision = self.reconcile()
        self.assertNotEqual(revision.pk, active.pk)
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertEqual(self.project.active_revision_id, active.pk)

    def test_a_file_added_to_the_source_reaches_what_is_served(self):
        self.reconcile()
        data_file(self.source, 'scripts/helper.py', b'VALUE = 1\n')

        with_helper = self.reconcile()
        self.assertEqual(sorted(entry['path'] for entry in with_helper.manifest), ['deploy.py', 'helper.py'])
        self.assertEqual(self.project.active_revision_id, with_helper.pk)

    def test_a_source_reverted_to_an_earlier_tree_serves_that_revision_again(self):
        # Content addressing hands back the revision that already validated this tree, and no
        # validation can claim a revision holding a verdict, so activation is the only step left.
        # Without it a revert in the source would silently change nothing.
        first = self.reconcile()
        data_file(self.source, 'scripts/helper.py', b'VALUE = 1\n')
        with_helper = self.reconcile()
        self.assertEqual(self.project.active_revision_id, with_helper.pk)

        DataFile.objects.filter(path='scripts/helper.py').delete()
        job = self.run_reconciliation()

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(self.project.active_revision_id, first.pk)
        first.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.ACTIVE)
        # The tree was staged once, so the revert reuses the row rather than adding a third.
        self.assertEqual(ScriptProjectRevision.objects.filter(project=self.project).count(), 2)

    def test_a_revert_on_a_manual_project_waits_for_an_operator(self):
        first = self.reconcile()
        data_file(self.source, 'scripts/helper.py', b'VALUE = 1\n')
        with_helper = self.reconcile()
        ScriptProject.objects.filter(pk=self.project.pk).update(activation_policy=ActivationPolicyChoices.MANUAL)
        DataFile.objects.filter(path='scripts/helper.py').delete()

        job = self.run_reconciliation()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertIn('waiting for an operator', str(job.log_entries))
        self.assertEqual(self.project.active_revision_id, with_helper.pk)
        # Retired, so the Activate button offers it.
        self.assertEqual(self.project.activatable_revision().pk, first.pk)

    def test_an_unchanged_source_leaves_the_active_revision_alone(self):
        active = self.reconcile()

        job = self.run_reconciliation()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertIn('has not changed', str(job.log_entries))
        self.assertEqual(self.project.active_revision_id, active.pk)
        self.assertEqual(ScriptProjectRevision.objects.filter(project=self.project).count(), 1)
