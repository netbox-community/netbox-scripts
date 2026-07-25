import contextlib
import hashlib
import pathlib
import tempfile
from unittest import mock

from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.test import TestCase, override_settings

from netbox_custom_scripts import signals
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage import store
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.storage.paths import project_directory, revision_directory

LOGGER = 'netbox.plugins.netbox_custom_scripts.storage'


def manifest_for(source):
    """Return manifest entries for a source mapping, as build_manifest would."""
    return [
        {'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
        for path, body in sorted(source.items())
    ]


# Two distinct trees, because a revision directory is named by the digest of its content and
# the store now refuses a digest that does not address the manifest stored under it.
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
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'project_root': self.root}}))
        self.project = CustomScriptProject.objects.create(name='Cleanup Project', key='cleanup-project')

    def project_path(self, project=None):
        return project_directory(self.root, (project or self.project).storage_key)

    def revision_path(self, digest, project=None):
        return revision_directory(self.root, (project or self.project).storage_key, digest)

    def make_revision(self, digest, on_disk=True, project=None):
        project = project or self.project
        if on_disk:
            source, entries = STORED[digest]
            store.write_staged_revision(self.root, project.storage_key, digest, source, entries)
        status = RevisionStatusChoices.VALID if digest else RevisionStatusChoices.INVALID
        return CustomScriptProjectRevision.objects.create(project=project, digest=digest, status=status)


class CleanupSignalsTestCase(CleanupFixtureMixin, TestCase):
    def test_deleting_revision_removes_its_digest_directory(self):
        first = self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.captureOnCommitCallbacks(execute=True):
            first.delete()
        self.assertFalse(self.revision_path(DIGEST_A).exists())
        self.assertTrue(self.revision_path(DIGEST_B).is_dir())

    def test_deleting_project_removes_entire_storage_key_tree(self):
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        stored = self.project_path()
        self.assertTrue(stored.is_dir())
        with self.captureOnCommitCallbacks(execute=True):
            self.project.delete()
        self.assertFalse(stored.exists())

    def test_deleting_invalid_revision_schedules_no_cleanup(self):
        revision = self.make_revision(None, on_disk=False)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            revision.delete()
        self.assertEqual(callbacks, [])

    def test_unsafe_branching_routing_leaves_the_source_on_disk(self):
        # The row deleted here may not be the only row naming this source, so removing it could
        # take content away from a schema that still serves it. Leaking a directory is
        # recoverable, deleting live source is not.
        revision = self.make_revision(DIGEST_A)
        stored = self.revision_path(DIGEST_A)
        with (
            mock.patch.object(signals.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertLogs(signals.logger, 'ERROR') as logged,
            self.captureOnCommitCallbacks(execute=True),
        ):
            revision.delete()
        self.assertTrue(stored.is_dir())
        self.assertIn('leaving source on disk', logged.output[0])

    def test_unsafe_branching_routing_leaves_a_deleted_projects_tree_on_disk(self):
        self.make_revision(DIGEST_A)
        stored = self.project_path()
        with (
            mock.patch.object(signals.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertLogs(signals.logger, 'ERROR'),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.project.delete()
        self.assertTrue(stored.is_dir())

    def test_cleanup_tolerates_already_missing_directories(self):
        revision = self.make_revision(DIGEST_A, on_disk=False)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            revision.delete()
        self.assertEqual(len(callbacks), 1)
        self.assertFalse(self.revision_path(DIGEST_A).exists())

    def test_cleanup_does_not_run_if_transaction_rolls_back(self):
        revision = self.make_revision(DIGEST_A)
        # Django clears the instance pk on delete, so the row is looked up by a saved copy.
        revision_pk = revision.pk
        # The contexts exit in reverse order, so the savepoint rolls back and drops its
        # queued callback before the capture block would have executed it.
        with (
            self.captureOnCommitCallbacks(execute=True) as callbacks,
            contextlib.suppress(RuntimeError),
            transaction.atomic(),
        ):
            revision.delete()
            raise RuntimeError('rolled back on purpose')
        self.assertEqual(callbacks, [])
        self.assertTrue(self.revision_path(DIGEST_A).is_dir())
        self.assertTrue(CustomScriptProjectRevision.objects.filter(pk=revision_pk).exists())

    def test_cleanup_failure_does_not_stop_later_callbacks(self):
        # The rows are already gone once these run, so one failing removal must not take the
        # others with it.
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        calls = []

        def flaky(project_root, storage_key, digest):
            calls.append(digest)
            if len(calls) == 1:
                raise PermissionError('read-only file system')

        with (
            mock.patch.object(store, 'delete_revision_directory', flaky),
            self.assertLogs(LOGGER, level='WARNING') as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.project.delete()

        self.assertEqual(len(calls), 2)
        self.assertTrue(any('cleanup failed' in message for message in logs.output))
        self.assertFalse(self.project_path().exists())

    def test_cleanup_registers_on_the_alias_that_performed_the_delete(self):
        # on_commit runs a callback immediately when its connection has no open transaction,
        # so registering on the wrong alias would delete a tree before the delete commits.
        self.make_revision(DIGEST_A)
        expected = router.db_for_write(CustomScriptProject, instance=self.project)
        with mock.patch('netbox_custom_scripts.signals.transaction.on_commit') as on_commit:
            self.project.delete()
        self.assertTrue(on_commit.call_args_list)
        for call in on_commit.call_args_list:
            self.assertEqual(call.kwargs['using'], expected)
            self.assertTrue(call.kwargs['robust'])

    def test_project_cleanup_threads_the_alias_the_signal_supplied(self):
        # A foreign alias shows the value is carried through from the signal rather than
        # resolved again against the default connection.
        signals.capture_project_storage(CustomScriptProject, self.project, using=DEFAULT_DB_ALIAS)
        with mock.patch('netbox_custom_scripts.signals.transaction.on_commit') as on_commit:
            signals.cleanup_project_storage(CustomScriptProject, self.project, using='schema_example')
        self.assertEqual(on_commit.call_args.kwargs['using'], 'schema_example')
        self.assertTrue(on_commit.call_args.kwargs['robust'])

    def test_cleanup_is_skipped_when_nothing_was_captured(self):
        # post_delete without its pre_delete partner has no trustworthy identity to act on,
        # so it warns and removes nothing rather than guessing from the instance.
        with (
            self.assertLogs(LOGGER, level='WARNING') as logs,
            mock.patch('netbox_custom_scripts.signals.transaction.on_commit') as on_commit,
        ):
            signals.cleanup_project_storage(CustomScriptProject, self.project, using=DEFAULT_DB_ALIAS)
        on_commit.assert_not_called()
        self.assertTrue(any('was not captured' in message for message in logs.output))

    def test_cleanup_refuses_a_symlinked_project_directory(self):
        # A refusal arrives as a StorageError rather than an OSError, and it must be logged
        # and absorbed like any other failed removal instead of escaping the callback.
        outside = pathlib.Path(self.root) / 'outside'
        (outside / 'revisions' / DIGEST_A).mkdir(parents=True)
        (outside / 'revisions' / DIGEST_A / 'keep.py').write_bytes(b'not ours')
        revision = self.make_revision(DIGEST_A, on_disk=False)
        self.project_path().symlink_to(outside, target_is_directory=True)

        with (
            self.assertLogs(LOGGER, level='WARNING') as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            revision.delete()

        self.assertTrue((outside / 'revisions' / DIGEST_A / 'keep.py').exists())
        self.assertTrue(any('left content on disk' in message for message in logs.output))

    def test_cleanup_is_skipped_when_storage_is_not_configured(self):
        self.make_revision(DIGEST_A)
        stored = self.project_path()
        with (
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {}}),
            self.assertLogs(LOGGER, level='DEBUG') as logs,
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.project.delete()
        self.assertTrue(stored.is_dir())
        self.assertTrue(any('not configured' in message for message in logs.output))


class DeletionIdentityTestCase(CleanupFixtureMixin, TestCase):
    """
    Cover deletion cleanup being driven by persisted identity rather than by the instance.

    A delete removes the row named by the primary key, so the rest of the caller's instance
    can be stale or carry unsaved edits without the delete itself noticing. Cleanup that read
    those fields would remove another object's content.
    """

    def other_project(self):
        """Return a second project with its own revision tree already on disk."""
        other = CustomScriptProject.objects.create(name='Other Cleanup', key='other-cleanup')
        self.make_revision(DIGEST_A, project=other)
        return other

    def test_a_stale_project_instance_can_still_be_deleted(self):
        # The pointer is cleared unconditionally, so an instance loaded before another caller
        # activated a revision does not leave the row protected.
        revision = self.make_revision(DIGEST_A)
        stale = CustomScriptProject.objects.get(pk=self.project.pk)
        self.assertIsNone(stale.active_revision_id)

        revision.status = RevisionStatusChoices.ACTIVE
        revision.save()
        CustomScriptProject.objects.filter(pk=self.project.pk).update(active_revision=revision)

        with self.captureOnCommitCallbacks(execute=True):
            stale.delete()
        self.assertFalse(CustomScriptProject.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(self.project_path().exists())

    def test_a_mutated_storage_key_cannot_remove_another_projects_tree(self):
        other = self.other_project()
        other_tree = self.project_path(other)
        own_tree = self.project_path()
        self.make_revision(DIGEST_B)

        # storage_key is editable=False, so this is deliberate tampering rather than something
        # a form or serializer can do. Cleanup still has to take its own row's word for it.
        self.project.storage_key = other.storage_key
        with self.captureOnCommitCallbacks(execute=True):
            self.project.delete()

        self.assertTrue(other_tree.is_dir())
        self.assertTrue((other_tree / 'revisions' / DIGEST_A).is_dir())
        self.assertFalse(own_tree.exists())

    def test_a_mutated_digest_cannot_remove_another_revisions_tree(self):
        keep = self.make_revision(DIGEST_A)
        target = self.make_revision(DIGEST_B)

        target.digest = DIGEST_A
        with self.captureOnCommitCallbacks(execute=True):
            target.delete()

        self.assertTrue(self.revision_path(DIGEST_A).is_dir())
        self.assertFalse(self.revision_path(DIGEST_B).exists())
        self.assertTrue(CustomScriptProjectRevision.objects.filter(pk=keep.pk).exists())

    def test_a_mutated_project_cannot_remove_another_projects_revision(self):
        other = self.other_project()
        other_tree = revision_directory(self.root, other.storage_key, DIGEST_A)
        target = self.make_revision(DIGEST_B)

        target.project = other
        with self.captureOnCommitCallbacks(execute=True):
            target.delete()

        self.assertTrue(other_tree.is_dir())
        self.assertFalse(self.revision_path(DIGEST_B).exists())

    def test_a_cascade_removes_every_revision_directory(self):
        # The revision receiver reads its project while both rows are still present, which a
        # cascade guarantees only because every pre_delete runs before the first delete.
        self.make_revision(DIGEST_A)
        self.make_revision(DIGEST_B)
        with self.captureOnCommitCallbacks(execute=True):
            self.project.delete()
        self.assertFalse(self.project_path().exists())
