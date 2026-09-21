"""
Serialization coverage for the storage lifecycle.

These are the concurrency scenarios the storage review asked for. They run as
TransactionTestCase, because a second database session can only observe rows this one has
committed, and a TestCase would hide every write inside a transaction that never commits.

Serialization is proved in two halves rather than by racing threads, which cannot be made
deterministic and would need a timeout to fail. The first half shows that each operation holds
the project lock across the exact span where interference would be harmful, observed from a
genuinely separate session. The second half shows the decision each operation makes there is
correct for every surviving ordering, with the interference injected at the seam. Together
those say the harmful interleaving cannot occur and everything still possible is handled.
"""

import hashlib
import uuid
from unittest import mock

import django_rq
from django.db.models.signals import post_save
from django.test import TransactionTestCase, override_settings

from core.models import Job
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.jobs import ProjectStorageCleanupJob
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, service, store
from netbox_scripts.storage.exceptions import RevisionVanishedError
from netbox_scripts.storage.manifest import compute_digest
from netbox_scripts.storage.paths import revision_prefix
from netbox_scripts.tests.plugin_testing import IN_MEMORY_STORAGES, SecondSession

SOURCE = {'hello.py': b'print("hi")\n'}


def manifest_for(files):
    """Return the manifest a source mapping would produce, without staging it."""
    return sorted(
        ({'path': path, 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()} for path, body in files.items()),
        key=lambda entry: entry['path'],
    )


class SerializationTestCase(TransactionTestCase):
    """Shared fixture: one upload project whose content can be staged and reclaimed."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        # This case commits, so the revision-deletion signal's on_commit callback really enqueues a
        # cleanup job. The queue is isolated by testing/configuration.py, and draining it here means
        # the suite leaves nothing behind even there.
        self.addCleanup(django_rq.get_queue('default').empty)
        self.storage = config.get_storage()
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')

    def keys_present(self, digest, paths=('hello.py',)):
        """Return the stored paths of one revision's tree that the backend still holds."""
        prefix = revision_prefix(self.project.storage_key, digest)
        return {path for path in paths if self.storage.exists(f'{prefix}{path}')}

    def materialize(self, files=None):
        """Stage one revision and return it."""
        return service.stage_revision(self.project, files or SOURCE).revision

    def runner(self):
        """Return a cleanup runner bound to a real Job row, the way handle() builds one."""
        return ProjectStorageCleanupJob(Job.objects.create(name='cleanup-test', job_id=uuid.uuid4()))

    def cleanup(self, digest, paths):
        """Drive the cleanup job body the way handle() does."""
        return self.runner().run(storage_key=str(self.project.storage_key), digest=digest, paths=paths, job_id='x')


class ScriptEditLockScopeTestCase(SerializationTestCase):
    """An ordinary Script edit serializes on its own row, never on the project."""

    def observe_project_lock_during_save(self, instance):
        """Save one row and report whether another session could take the project lock meanwhile."""
        observed = {}

        def observe(sender, **kwargs):
            with SecondSession() as other:
                observed['free'] = other.can_lock(self.project.storage_key)

        post_save.connect(observe, sender=type(instance))
        self.addCleanup(post_save.disconnect, observe, sender=type(instance))
        instance.save()
        return observed.get('free')

    def test_an_edit_does_not_wait_on_the_project_lock(self):
        # Activation holds the project lock across a whole-tree hash, so an edit that took it
        # would stall for as long as the backend needs.
        script = NetBoxScript.objects.create(
            project=self.project, module_path='deploy', class_name='Deploy', display_name='Deploy'
        )
        script.enabled = False

        self.assertTrue(self.observe_project_lock_during_save(script), 'The edit took the project lock.')

    def test_a_declaration_still_takes_the_project_lock(self):
        # The control: Script Files serialize their path invariants on the project, unchanged.
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        script_file.description = 'Edited'

        self.assertFalse(self.observe_project_lock_during_save(script_file), 'The declaration lost its lock.')


class ProjectLockHeldTestCase(SerializationTestCase):
    """Every operation that touches stored content holds the project lock while it does."""

    def assert_locked_during(self, seam, operation):
        """Run an operation and check another session cannot take the lock at the named seam."""
        observed = {}
        real = getattr(store, seam)

        def observe(*args, **kwargs):
            with SecondSession() as other:
                observed['locked'] = not other.can_lock(self.project.storage_key)
            return real(*args, **kwargs)

        with mock.patch.object(store, seam, side_effect=observe):
            operation()
        self.assertTrue(observed.get('locked'), 'The project lock was not held at the seam.')
        with SecondSession() as other:
            self.assertTrue(other.can_lock(self.project.storage_key), 'The project lock outlived the operation.')

    def test_staging_holds_the_lock_across_the_content_write(self):
        self.assert_locked_during('write_revision', self.materialize)

    def test_staging_holds_the_lock_while_verifying_an_existing_tree(self):
        self.materialize()
        self.assert_locked_during('verify_revision_tree', self.materialize)

    def test_activation_holds_the_lock_across_the_verification(self):
        revision = self.materialize()
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        revision.refresh_from_db()
        self.assert_locked_during(
            'verify_revision_tree',
            lambda: service.promote_revision(revision, on_promote=lambda **kwargs: None),
        )

    def test_script_file_refresh_holds_the_lock_across_the_verification(self):
        revision = self.materialize()
        ScriptFile.objects.create(project=self.project, source_path='hello.py', enabled=True)
        self.assert_locked_during('verify_revision_tree', lambda: service.refresh_revision_script_files(revision))

    def test_cleanup_holds_the_lock_across_the_removal(self):
        revision = self.materialize()
        digest, paths = revision.digest, [entry['path'] for entry in revision.manifest]
        revision.delete()
        self.assert_locked_during('delete_revision', lambda: self.cleanup(digest, paths))


class CleanupVersusRestageTestCase(SerializationTestCase):
    """Scenario 1: content a live revision references is never reclaimed."""

    def test_a_surviving_reference_keeps_the_content_and_the_run_succeeds(self):
        # Stands in for a restage that committed before this run reached its recheck.
        revision = self.materialize()
        digest, paths = revision.digest, [entry['path'] for entry in revision.manifest]
        self.cleanup(digest, paths)
        self.assertEqual(self.keys_present(digest), {'hello.py'})

    def test_content_no_row_references_is_reclaimed(self):
        revision = self.materialize()
        digest, paths = revision.digest, [entry['path'] for entry in revision.manifest]
        revision.delete()
        self.cleanup(digest, paths)
        self.assertEqual(self.keys_present(digest), set())

    def test_a_reference_under_a_second_identity_keeps_the_content(self):
        # Script File configuration is part of revision identity, so one tree can carry several
        # rows. Reclaiming on the first delete would take content the survivor still names.
        revision = self.materialize()
        ScriptFile.objects.create(project=self.project, source_path='hello.py', enabled=True)
        sibling = service.refresh_revision_script_files(revision).revision
        self.assertNotEqual(sibling.pk, revision.pk)

        digest, paths = revision.digest, [entry['path'] for entry in revision.manifest]
        revision.delete()
        self.cleanup(digest, paths)
        self.assertEqual(self.keys_present(digest), {'hello.py'})


class DuplicateStagingTestCase(SerializationTestCase):
    """Scenario 2: two stagers on one digest settle on one row and one stored tree."""

    def test_identical_content_resolves_to_one_revision(self):
        first = self.materialize()
        second = self.materialize()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(ScriptProjectRevision.objects.count(), 1)
        self.assertEqual(self.keys_present(first.digest), {'hello.py'})

    def test_a_row_another_stager_already_created_is_adopted(self):
        # The identity row is created under the lock, so the loser of the insert race reaches
        # the existing row and verifies its tree instead of writing a second one.
        entries = manifest_for(SOURCE)
        digest = compute_digest(entries)
        ScriptProjectRevision.objects.create(
            project=self.project,
            digest=digest,
            status=RevisionStatusChoices.MATERIALIZED,
            manifest=entries,
            file_count=len(entries),
            total_size=sum(entry['size'] for entry in entries),
        )
        store.write_revision(self.storage, self.project.storage_key, digest, SOURCE, entries)

        staged = service.stage_revision(self.project, SOURCE)
        self.assertFalse(staged.created)
        self.assertEqual(ScriptProjectRevision.objects.count(), 1)


class SlowStagerTestCase(SerializationTestCase):
    """Scenarios 3 and 4: a slow stager cannot demote a row validation or activation advanced."""

    def test_a_stager_cannot_demote_a_validating_revision(self):
        revision = self.materialize()
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALIDATING)
        # VALIDATING is neither stored nor retryable, so staging returns the row untouched.
        staged = service.stage_revision(self.project, SOURCE)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.VALIDATING)

    def test_a_stager_cannot_pull_a_revision_back_from_active(self):
        revision = self.materialize()
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        revision.refresh_from_db()
        service.promote_revision(revision, on_promote=lambda **kwargs: None)

        staged = service.stage_revision(self.project, SOURCE)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.ACTIVE)
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_a_write_that_lost_its_window_does_not_settle_the_row(self):
        # The status advances while the write is in flight, so the closing conditional update
        # matches nothing and the other owner's word stands.
        real_write = store.write_revision

        def write_then_advance(*args, **kwargs):
            result = real_write(*args, **kwargs)
            ScriptProjectRevision.objects.filter(project=self.project).update(status=RevisionStatusChoices.VALIDATING)
            return result

        with mock.patch.object(store, 'write_revision', side_effect=write_then_advance):
            staged = service.stage_revision(self.project, SOURCE)
        self.assertEqual(staged.revision.status, RevisionStatusChoices.VALIDATING)


class DeletionVersusStagingTestCase(SerializationTestCase):
    """Scenario 5: a project deleted mid-write leaves no revision claiming missing content."""

    def delete_project_after_write(self):
        """Patch the content write so the owning project is cascaded away right after it."""
        real_write = store.write_revision

        def write_then_delete(*args, **kwargs):
            result = real_write(*args, **kwargs)
            # QuerySet.delete() skips the model's pointer clear, so it is done here.
            ScriptProject.objects.filter(pk=self.project.pk).update(active_revision=None)
            ScriptProject.objects.filter(pk=self.project.pk).delete()
            return result

        return mock.patch.object(store, 'write_revision', side_effect=write_then_delete)

    def test_a_project_deleted_at_the_write_seam_reports_the_vanished_revision(self):
        # Deletion takes no project lock, by design: the cleanup job is what decides whether
        # content is still claimed, under the lock. So this cascade can land mid-write, and the
        # staging call has to say so rather than return a row that no longer exists.
        with (
            mock.patch.object(ProjectStorageCleanupJob, 'enqueue_cleanup', return_value=None),
            self.delete_project_after_write(),
            self.assertRaises(RevisionVanishedError) as ctx,
        ):
            service.stage_revision(self.project, SOURCE)
        self.assertIn('deleted while', str(ctx.exception))
        self.assertFalse(ScriptProjectRevision.objects.exists())

    def test_the_content_a_vanished_revision_wrote_is_reclaimable(self):
        # Nothing leaks permanently. The cleanup the delete recorded finds no referencing row
        # and reclaims the tree, including bytes written after the cascade committed.
        digest = compute_digest(manifest_for(SOURCE))
        paths = [entry['path'] for entry in manifest_for(SOURCE)]
        with (
            mock.patch.object(ProjectStorageCleanupJob, 'enqueue_cleanup', return_value=None),
            self.delete_project_after_write(),
            self.assertRaises(RevisionVanishedError),
        ):
            service.stage_revision(self.project, SOURCE)
        self.assertEqual(self.keys_present(digest), {'hello.py'})

        self.cleanup(digest, paths)
        self.assertEqual(self.keys_present(digest), set())
