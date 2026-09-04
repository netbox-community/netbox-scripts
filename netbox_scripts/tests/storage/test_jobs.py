import contextlib
import hashlib
import uuid
from datetime import timedelta
from unittest import mock

from django.db import transaction
from django.test import TestCase, override_settings
from django.utils import timezone
from rq.timeouts import JobTimeoutException

from core.choices import JobIntervalChoices, JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from netbox.registry import registry
from netbox_scripts import jobs
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.jobs import ProjectStorageCleanupJob
from netbox_scripts.models import CustomScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, store
from netbox_scripts.storage.exceptions import StorageError
from netbox_scripts.storage.manifest import compute_digest
from netbox_scripts.storage.paths import revision_prefix

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

STORAGE_KEY = '9f1c6d24-0b2a-4d3e-8f57-2c9a4b6e1d80'
SOURCE = {'hello.py': b'print("hi")\n', 'pkg/util.py': b'VALUE = 1\n'}
MANIFEST = sorted(
    ({'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()} for path, body in SOURCE.items()),
    key=lambda entry: entry['path'],
)
DIGEST = compute_digest(MANIFEST)
PATHS = [entry['path'] for entry in MANIFEST]


class ProjectStorageCleanupJobTestCase(TestCase):
    """Cover the job that reclaims a deleted revision's stored content."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.storage = config.get_storage()
        store.write_revision(self.storage, STORAGE_KEY, DIGEST, SOURCE, MANIFEST)

    def runner(self):
        """Return a runner bound to a real Job row, the way handle() builds one."""
        return ProjectStorageCleanupJob(Job.objects.create(name='cleanup-test', job_id=uuid.uuid4()))

    def run_job(self, runner=None):
        """Drive the job body the way handle() does, including the job_id RQ passes along."""
        (runner or self.runner()).run(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS, job_id='x')

    def keys_present(self):
        prefix = revision_prefix(STORAGE_KEY, DIGEST)
        return {path for path in PATHS if self.storage.exists(f'{prefix}{path}')}

    def test_the_job_removes_every_manifest_key(self):
        self.run_job()
        self.assertEqual(self.keys_present(), set())

    def test_a_second_run_on_reclaimed_content_succeeds(self):
        # The rows are gone by the time the job runs, so a repeated or requeued job has to
        # treat keys that are already gone as removed.
        self.run_job()
        self.run_job()
        self.assertEqual(self.keys_present(), set())

    def test_a_partial_failure_fails_the_job_and_still_attempts_every_key(self):
        storage = self.storage

        class RefusingOne:
            def delete(self, name):
                if name.endswith('pkg/util.py'):
                    raise OSError('the backend refused the removal')
                storage.delete(name)

        runner = self.runner()
        with (
            mock.patch.object(jobs.config, 'get_storage', return_value=RefusingOne()),
            self.assertRaises(JobFailed) as ctx,
        ):
            self.run_job(runner)
        self.assertIsInstance(ctx.exception.__cause__, StorageError)
        self.assertIn('pkg/util.py', str(ctx.exception.__cause__))
        self.assertEqual(self.keys_present(), {'pkg/util.py'})
        # The detail lands in the job log, the record an operator reads once the rows are gone.
        self.assertIn('pkg/util.py', str(runner.job.log_entries))

    def test_a_missing_storage_entry_fails_the_run(self):
        no_entry = {key: value for key, value in IN_MEMORY_STORAGES.items() if key != 'netbox_scripts'}
        runner = self.runner()
        with override_settings(STORAGES=no_entry), self.assertRaises(JobFailed):
            self.run_job(runner)
        self.assertIn('not configured', str(runner.job.log_entries))

    def test_an_immediate_enqueue_completes_and_reclaims_the_content(self):
        # immediate=True drives the full handle flow synchronously: the Job row is created,
        # run() receives the enqueue kwargs, and the terminal status lands on the row.
        job = ProjectStorageCleanupJob.enqueue(immediate=True, storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(self.keys_present(), set())

    def test_a_failed_run_lands_on_the_job_row_as_failed(self):
        # JobFailed is the convention for an expected operational failure, so the row reads
        # failed rather than errored, with the job log carrying the detail.
        no_entry = {key: value for key, value in IN_MEMORY_STORAGES.items() if key != 'netbox_scripts'}
        with override_settings(STORAGES=no_entry):
            job = ProjectStorageCleanupJob.enqueue(immediate=True, storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertEqual(self.keys_present(), set(PATHS))

    def test_unsafe_routing_fails_the_run_and_leaves_every_key(self):
        # Routing was safe when the deletion recorded this job. The destructive step may
        # run much later on another pod, so it repeats the check and fails closed,
        # keeping the content and the payload in Job.data as the retry inventory.
        with self.captureOnCommitCallbacks():
            job = ProjectStorageCleanupJob.enqueue_cleanup(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
        job.refresh_from_db()
        runner = ProjectStorageCleanupJob(job)
        with (
            mock.patch.object(jobs.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertRaises(JobFailed),
        ):
            runner.run(job_id='later', **job.data)
        self.assertEqual(self.keys_present(), set(PATHS))
        # Refreshing resets the in-memory log the handler wrote, so read it first.
        self.assertIn('Routing is unsafe.', str(runner.job.log_entries))
        job.refresh_from_db()
        self.assertEqual(job.data, {'storage_key': STORAGE_KEY, 'digest': DIGEST, 'paths': PATHS})

    def test_enqueue_cleanup_persists_the_payload_before_the_queue_handoff(self):
        # The commit hook that hands the task to the queue has not run inside this block, so
        # the payload on the row is what survives a lost queue entry or a failed handoff.
        with self.captureOnCommitCallbacks() as callbacks:
            job = ProjectStorageCleanupJob.enqueue_cleanup(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
        self.assertEqual(len(callbacks), 1)
        job.refresh_from_db()
        self.assertEqual(job.data, {'storage_key': STORAGE_KEY, 'digest': DIGEST, 'paths': PATHS})

    def test_the_persisted_payload_alone_reconstructs_the_cleanup(self):
        with self.captureOnCommitCallbacks():
            job = ProjectStorageCleanupJob.enqueue_cleanup(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
        job.refresh_from_db()
        ProjectStorageCleanupJob(job).run(job_id='retry', **job.data)
        self.assertEqual(self.keys_present(), set())

    def test_a_rolled_back_caller_transaction_discards_the_job(self):
        # The atomic block inside enqueue_cleanup nests in the caller's transaction, so a
        # caller that rolls back takes the Job, its payload, and the queue callback with it.
        with (
            self.captureOnCommitCallbacks() as callbacks,
            contextlib.suppress(RuntimeError),
            transaction.atomic(),
        ):
            ProjectStorageCleanupJob.enqueue_cleanup(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS)
            raise RuntimeError('rolled back on purpose')
        self.assertEqual(callbacks, [])
        self.assertEqual(Job.objects.count(), 0)


class SharedDigestJobTestCase(TestCase):
    """The run-time reference recheck, for content re-staged after its cleanup was recorded."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.storage = config.get_storage()
        store.write_revision(self.storage, STORAGE_KEY, DIGEST, SOURCE, MANIFEST)

    def test_a_current_reference_leaves_the_content_and_succeeds(self):
        project = CustomScriptProject.objects.create(
            name='Cleanup Reference Project',
            key='cleanup-reference-project',
            storage_key=STORAGE_KEY,
        )
        ScriptProjectRevision.objects.create(
            project=project,
            digest=DIGEST,
            status=RevisionStatusChoices.VALID,
            manifest=MANIFEST,
        )
        runner = ProjectStorageCleanupJob(Job.objects.create(name='cleanup-reference', job_id=uuid.uuid4()))
        runner.run(storage_key=STORAGE_KEY, digest=DIGEST, paths=PATHS, job_id='x')
        prefix = revision_prefix(STORAGE_KEY, DIGEST)
        self.assertEqual({path for path in PATHS if self.storage.exists(f'{prefix}{path}')}, set(PATHS))
        self.assertIn('references it again', str(runner.job.log_entries))


class ProjectStorageSweepJobTestCase(TestCase):
    """Cover the sweep that reports content an unfinished cleanup left in the store."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.storage = config.get_storage()
        store.write_revision(self.storage, STORAGE_KEY, DIGEST, SOURCE, MANIFEST)
        self.project = CustomScriptProject.objects.create(
            name='Sweep Project',
            key='sweep-project',
            storage_key=STORAGE_KEY,
        )

    def cleanup_job(self, *, status, age_hours, data=None):
        """Create a cleanup Job row in a given state, backdating created past auto_now_add."""
        job = Job.objects.create(
            name=ProjectStorageCleanupJob.name,
            job_id=uuid.uuid4(),
            status=status,
            data={'storage_key': STORAGE_KEY, 'digest': DIGEST, 'paths': PATHS} if data is None else data,
        )
        Job.objects.filter(pk=job.pk).update(created=timezone.now() - timedelta(hours=age_hours))
        return job

    def runner(self):
        """Return a sweep runner bound to a real Job row, the way handle() builds one."""
        return jobs.ProjectStorageSweepJob(Job.objects.create(name='sweep-test', job_id=uuid.uuid4()))

    def sweep(self):
        """Run one sweep and return the report it recorded."""
        runner = self.runner()
        runner.run(job_id='x')
        return runner.job.data

    def test_the_sweep_is_registered_as_a_daily_system_job(self):
        self.assertIn(jobs.ProjectStorageSweepJob, registry['system_jobs'])
        self.assertEqual(
            registry['system_jobs'][jobs.ProjectStorageSweepJob]['interval'],
            JobIntervalChoices.INTERVAL_DAILY,
        )

    def test_an_old_pending_cleanup_reports_its_content_as_stranded(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_PENDING, age_hours=4)
        report = self.sweep()
        self.assertEqual([entry['jobs'] for entry in report['stranded']], [[job.pk]])
        self.assertEqual(report['stranded'][0]['paths'], PATHS)

    def test_a_failed_cleanup_reports_its_content_without_waiting_out_the_grace(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        self.assertEqual([entry['jobs'] for entry in self.sweep()['stranded']], [[job.pk]])

    def test_several_cleanups_naming_one_tree_are_reported_once(self):
        # Django deletes the whole batch before any post_delete fires, so the receiver's sibling
        # guard never sees a survivor and each row enqueues its own cleanup.
        first = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        second = self.cleanup_job(status=JobStatusChoices.STATUS_PENDING, age_hours=4)
        report = self.sweep()
        self.assertEqual(len(report['stranded']), 1)
        self.assertEqual(report['stranded'][0]['jobs'], sorted((first.pk, second.pk)))
        self.assertEqual(report['stranded'][0]['paths'], PATHS)

    def test_a_recent_pending_cleanup_is_left_alone(self):
        self.cleanup_job(status=JobStatusChoices.STATUS_PENDING, age_hours=0)
        report = self.sweep()
        self.assertEqual(report['stranded'], [])
        self.assertEqual(report['reclaimed'], [])

    def test_a_completed_cleanup_is_never_a_candidate(self):
        self.cleanup_job(status=JobStatusChoices.STATUS_COMPLETED, age_hours=4)
        self.assertEqual(self.sweep()['stranded'], [])

    def test_content_a_revision_names_again_is_referenced_rather_than_stranded(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_PENDING, age_hours=4)
        ScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST,
            status=RevisionStatusChoices.VALID,
            manifest=MANIFEST,
        )
        report = self.sweep()
        self.assertEqual(report['stranded'], [])
        self.assertEqual([entry['jobs'] for entry in report['referenced']], [[job.pk]])

    def test_a_cleanup_whose_content_is_already_gone_reports_as_reclaimed(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        store.delete_revision(self.storage, STORAGE_KEY, DIGEST, PATHS)
        report = self.sweep()
        self.assertEqual(report['stranded'], [])
        self.assertEqual([entry['jobs'] for entry in report['reclaimed']], [[job.pk]])

    def test_a_cleanup_with_no_usable_payload_is_reported_as_unreadable(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0, data={})
        self.assertEqual(self.sweep()['unreadable'], [job.pk])

    def test_a_backend_that_cannot_answer_leaves_the_sweep_running(self):
        job = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        with mock.patch.object(store, 'present_keys', side_effect=StorageError('unreachable')):
            report = self.sweep()
        self.assertEqual(report['unreadable'], [job.pk])
        self.assertEqual(report['stranded'], [])

    def test_the_sweep_reclaims_nothing(self):
        self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        self.sweep()
        prefix = revision_prefix(STORAGE_KEY, DIGEST)
        self.assertEqual({path for path in PATHS if self.storage.exists(f'{prefix}{path}')}, set(PATHS))

    def test_a_sweep_cut_short_still_records_what_it_classified(self):
        # The report has to reach the row before classification starts, not after it finishes.
        self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        runner = self.runner()
        with (
            mock.patch.object(runner, '_classify', side_effect=JobTimeoutException('killed')),
            self.assertRaises(JobTimeoutException),
        ):
            runner.run(job_id='x')
        self.assertEqual(runner.job.data['stranded'], [])
        self.assertEqual(runner.job.data['unreadable'], [])

    def test_candidates_are_classified_oldest_first(self):
        newer = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=1)
        older = self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=9)
        self.assertEqual(
            [job.pk for job in jobs.ProjectStorageSweepJob._stalled_cleanups()],
            [older.pk, newer.pk],
        )

    def test_the_recheck_happens_under_the_project_lock(self):
        self.cleanup_job(status=JobStatusChoices.STATUS_FAILED, age_hours=0)
        with mock.patch.object(jobs, 'project_lock', wraps=jobs.project_lock) as lock:
            self.sweep()
        lock.assert_called_once_with(STORAGE_KEY)
