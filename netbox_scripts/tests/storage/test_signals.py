import contextlib
import hashlib
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, transaction
from django.test import TestCase, override_settings

from core.models import Job
from netbox_scripts import signals
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.models import ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, store
from netbox_scripts.storage.exceptions import RevisionCorruptError
from netbox_scripts.storage.manifest import compute_digest
from netbox_scripts.storage.paths import revision_prefix

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

LOGGER = 'netbox.plugins.netbox_scripts.storage'


def manifest_for(source):
    """Return manifest entries for a source mapping, as build_manifest would."""
    return [
        {'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        for path, body in sorted(source.items())
    ]


# Two distinct trees, because a revision prefix is named by the digest of its content and
# the store refuses a digest that does not address the manifest stored under it.
SOURCE_A = {'hello.py': b'print("hi")\n'}
SOURCE_B = {'hello.py': b'print("bye")\n'}
MANIFEST_A = manifest_for(SOURCE_A)
MANIFEST_B = manifest_for(SOURCE_B)
DIGEST_A = compute_digest(MANIFEST_A)
DIGEST_B = compute_digest(MANIFEST_B)
STORED = {DIGEST_A: (SOURCE_A, MANIFEST_A), DIGEST_B: (SOURCE_B, MANIFEST_B)}


class CleanupFixtureMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.storage = config.get_storage()
        self.project = ScriptProject.objects.create(name='Cleanup Project', key='cleanup-project')

    def revision_stored(self, digest, project=None):
        """Report whether one revision's single source file is still stored."""
        prefix = revision_prefix((project or self.project).storage_key, digest)
        return self.storage.exists(f'{prefix}hello.py')

    def make_revision(self, digest, stored=True, project=None):
        project = project or self.project
        entries = STORED[digest][1] if digest else []
        if stored:
            source, entries = STORED[digest]
            store.write_revision(self.storage, project.storage_key, digest, source, entries)
        status = RevisionStatusChoices.VALID if digest else RevisionStatusChoices.INVALID
        return ScriptProjectRevision.objects.create(project=project, digest=digest, status=status, manifest=entries)

    def capture_enqueues(self):
        """Patch the cleanup job's durable enqueue, so a test asserts the handoff without RQ."""
        return mock.patch.object(signals.ProjectStorageCleanupJob, 'enqueue_cleanup')


class CleanupSignalsTestCase(CleanupFixtureMixin, TestCase):
    def test_deleting_a_revision_enqueues_cleanup_with_its_row_identity(self):
        first = self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.capture_enqueues() as enqueue:
            first.delete()
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST_A, paths=['hello.py'])

    def test_deleting_a_project_enqueues_cleanup_for_every_cascaded_revision(self):
        # Project deletion has no receiver of its own: the cascade collects each revision and
        # fires its receivers, because registering them rules out the fast-delete path.
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.capture_enqueues() as enqueue:
            self.project.delete()
        self.assertEqual(sorted(call.kwargs['digest'] for call in enqueue.call_args_list), sorted((DIGEST_A, DIGEST_B)))

    def test_deleting_an_invalid_revision_schedules_no_cleanup(self):
        revision = self.make_revision(None, stored=False)
        with self.capture_enqueues() as enqueue:
            revision.delete()
        enqueue.assert_not_called()

    def test_a_revision_with_no_manifest_entries_schedules_no_cleanup(self):
        # A manifest with no entries stored no keys, so a cleanup job would have nothing to do.
        revision = ScriptProjectRevision.objects.create(
            project=self.project, digest=compute_digest([]), status=RevisionStatusChoices.VALID, manifest=[]
        )
        with self.capture_enqueues() as enqueue:
            revision.delete()
        enqueue.assert_not_called()

    def test_a_malformed_captured_manifest_fails_the_delete_closed(self):
        # The captured manifest is the only inventory of the stored keys, so a shape this
        # receiver cannot trust aborts the delete rather than crashing or guessing.
        revision = self.make_revision(DIGEST_A)
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(manifest=[{'path': 'hello.py'}])
        with self.capture_enqueues() as enqueue, self.assertRaises(RevisionCorruptError), transaction.atomic():
            revision.delete()
        enqueue.assert_not_called()
        self.assertTrue(ScriptProjectRevision.objects.filter(pk=revision.pk).exists())
        self.assertTrue(self.revision_stored(DIGEST_A))

    def test_a_manifest_that_does_not_address_its_digest_fails_the_delete_closed(self):
        revision = self.make_revision(DIGEST_A)
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(manifest=MANIFEST_B)
        with self.capture_enqueues() as enqueue, self.assertRaises(RevisionCorruptError), transaction.atomic():
            revision.delete()
        enqueue.assert_not_called()
        self.assertTrue(ScriptProjectRevision.objects.filter(pk=revision.pk).exists())

    def test_unsafe_branching_routing_leaves_the_source_in_the_store(self):
        # The row deleted here may not be the only row naming this source, so removing it could
        # take content away from a schema that still serves it. Leaking content is recoverable,
        # deleting live source is not.
        revision = self.make_revision(DIGEST_A)
        with (
            mock.patch.object(signals.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.capture_enqueues() as enqueue,
            self.assertLogs(signals.logger, 'ERROR') as logged,
        ):
            revision.delete()
        enqueue.assert_not_called()
        self.assertTrue(self.revision_stored(DIGEST_A))
        self.assertIn('leaving source in the store', logged.output[0])

    def test_a_delete_on_a_non_default_alias_fails_closed(self):
        # The core Job API binds the row and its queue callback to the default connection,
        # so recording cleanup for another alias could commit it independently.
        revision = self.make_revision(DIGEST_A)
        signals.capture_revision_storage(ScriptProjectRevision, revision, using=DEFAULT_DB_ALIAS)
        with self.assertRaises(ImproperlyConfigured) as ctx:
            signals.cleanup_revision_storage(ScriptProjectRevision, revision, using='replica')
        self.assertIn('replica', str(ctx.exception))
        self.assertEqual(Job.objects.count(), 0)
        self.assertTrue(self.revision_stored(DIGEST_A))

    def test_unsafe_branching_routing_skips_before_the_alias_contract(self):
        # A branch alias under broken routing hits the settled skip-and-log path, the
        # alias contract only refuses deletes that branching would have allowed.
        revision = self.make_revision(DIGEST_A)
        signals.capture_revision_storage(ScriptProjectRevision, revision, using=DEFAULT_DB_ALIAS)
        with (
            mock.patch.object(signals.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertLogs(signals.logger, 'ERROR'),
        ):
            signals.cleanup_revision_storage(ScriptProjectRevision, revision, using='replica')
        self.assertEqual(Job.objects.count(), 0)
        self.assertTrue(self.revision_stored(DIGEST_A))

    def test_a_rolled_back_delete_rolls_the_cleanup_job_back_with_it(self):
        revision = self.make_revision(DIGEST_A)
        # Django clears the instance pk on delete, so the row is looked up by a saved copy.
        revision_pk = revision.pk
        # The Job row is written inside the deleting transaction, so the savepoint rollback
        # discards it together with the queue callback Job.enqueue() registered.
        with (
            self.captureOnCommitCallbacks() as callbacks,
            contextlib.suppress(RuntimeError),
            transaction.atomic(),
        ):
            revision.delete()
            self.assertEqual(Job.objects.count(), 1)
            raise RuntimeError('rolled back on purpose')
        self.assertEqual(callbacks, [])
        self.assertEqual(Job.objects.count(), 0)
        self.assertTrue(self.revision_stored(DIGEST_A))
        self.assertTrue(ScriptProjectRevision.objects.filter(pk=revision_pk).exists())

    def test_a_failed_cleanup_enqueue_rolls_back_the_deletion(self):
        # Recording cleanup intent is part of the deletion now. A Job that cannot be created
        # aborts the delete, which fails closed on the side of keeping rows and content.
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.capture_enqueues() as enqueue:
            enqueue.side_effect = RuntimeError('the Job row could not be created')
            with self.assertRaises(RuntimeError), transaction.atomic():
                self.project.delete()
        self.assertEqual(ScriptProjectRevision.objects.count(), 2)
        self.assertTrue(ScriptProject.objects.filter(pk=self.project.pk).exists())
        self.assertTrue(self.revision_stored(DIGEST_A))
        self.assertTrue(self.revision_stored(DIGEST_B))

    def test_the_cleanup_job_commits_with_the_deletion(self):
        # Callbacks have not run inside this block, so a process lost here has already
        # persisted the Job and its inventory. Only the queue handoff is still pending.
        revision = self.make_revision(DIGEST_A)
        with self.captureOnCommitCallbacks() as callbacks:
            revision.delete()
            self.assertFalse(ScriptProjectRevision.objects.exists())
            job = Job.objects.get()
            self.assertEqual(
                job.data,
                {'storage_key': str(self.project.storage_key), 'digest': DIGEST_A, 'paths': ['hello.py']},
            )
        self.assertEqual(len(callbacks), 1)

    def test_cleanup_is_skipped_when_nothing_was_captured(self):
        # post_delete without its pre_delete partner has no trustworthy identity to act on,
        # so it warns and records nothing rather than guessing from the instance.
        revision = self.make_revision(DIGEST_A)
        bare = ScriptProjectRevision(pk=revision.pk)
        with (
            self.assertLogs(LOGGER, level='WARNING') as logs,
            self.capture_enqueues() as enqueue,
        ):
            signals.cleanup_revision_storage(ScriptProjectRevision, bare, using=DEFAULT_DB_ALIAS)
        enqueue.assert_not_called()
        self.assertTrue(any('was not captured' in message for message in logs.output))


class DeletionIdentityTestCase(CleanupFixtureMixin, TestCase):
    """
    Cover deletion cleanup being driven by persisted identity rather than by the instance.

    A delete removes the row named by the primary key, so the rest of the caller's instance
    can be stale or carry unsaved edits without the delete itself noticing. Cleanup that read
    those fields would enqueue the removal of another object's content.
    """

    def other_project(self):
        """Return a second project with its own revision tree already stored."""
        other = ScriptProject.objects.create(name='Other Cleanup', key='other-cleanup')
        self.make_revision(DIGEST_A, project=other)
        return other

    def test_a_stale_project_instance_can_still_be_deleted(self):
        # The pointer is cleared unconditionally, so an instance loaded before another caller
        # activated a revision does not leave the row protected.
        revision = self.make_revision(DIGEST_A)
        stale = ScriptProject.objects.get(pk=self.project.pk)
        self.assertIsNone(stale.active_revision_id)

        revision.status = RevisionStatusChoices.ACTIVE
        revision.save()
        ScriptProject.objects.filter(pk=self.project.pk).update(active_revision=revision)

        with self.capture_enqueues() as enqueue:
            stale.delete()
        self.assertFalse(ScriptProject.objects.filter(pk=self.project.pk).exists())
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST_A, paths=['hello.py'])

    def test_a_mutated_storage_key_cannot_remove_another_projects_content(self):
        other = self.other_project()
        self.make_revision(DIGEST_B)
        own_key = self.project.storage_key

        # storage_key is editable=False, so this is deliberate tampering rather than something
        # a form or serializer can do. Cleanup still has to take its own row's word for it.
        self.project.storage_key = other.storage_key
        with self.capture_enqueues() as enqueue:
            self.project.delete()
        enqueue.assert_called_once_with(storage_key=own_key, digest=DIGEST_B, paths=['hello.py'])

    def test_a_mutated_digest_cannot_remove_another_revisions_content(self):
        keep = self.make_revision(DIGEST_A)
        target = self.make_revision(DIGEST_B)

        target.digest = DIGEST_A
        with self.capture_enqueues() as enqueue:
            target.delete()
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST_B, paths=['hello.py'])
        self.assertTrue(ScriptProjectRevision.objects.filter(pk=keep.pk).exists())

    def test_a_mutated_project_cannot_remove_another_projects_revision(self):
        other = self.other_project()
        target = self.make_revision(DIGEST_B)

        target.project = other
        with self.capture_enqueues() as enqueue:
            target.delete()
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST_B, paths=['hello.py'])

    def test_a_cascade_enqueues_cleanup_for_every_revision(self):
        # The revision receiver reads its project while both rows are still present, which a
        # cascade guarantees only because every pre_delete runs before the first delete.
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.capture_enqueues() as enqueue:
            self.project.delete()
        self.assertEqual(enqueue.call_count, 2)


class SharedDigestCleanupTestCase(CleanupFixtureMixin, TestCase):
    """Rows sharing one stored tree since script file configuration joined revision identity."""

    def sibling(self):
        """Return a second row referencing DIGEST_A under another script file configuration."""
        return ScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.VALID,
            manifest=MANIFEST_A,
            script_file_digest='b' * 64,
        )

    def test_deleting_one_of_two_rows_sharing_content_skips_cleanup(self):
        first = self.make_revision(DIGEST_A)
        self.sibling()
        with self.capture_enqueues() as enqueue:
            first.delete()
        enqueue.assert_not_called()
        self.assertTrue(self.revision_stored(DIGEST_A))

    def test_deleting_the_last_referencing_row_enqueues_cleanup(self):
        first = self.make_revision(DIGEST_A)
        sibling = self.sibling()
        with self.capture_enqueues() as enqueue:
            sibling.delete()
            first.delete()
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST_A, paths=['hello.py'])
