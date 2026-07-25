import pathlib
import tempfile
from collections.abc import Mapping
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, router, transaction
from django.db.utils import ConnectionDoesNotExist
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage import service, store
from netbox_custom_scripts.storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from netbox_custom_scripts.storage.paths import project_directory, revision_directory

GOOD_FILES = {'hello.py': b'print("hi")\n', 'pkg/mod.py': b'VALUE = 1\n'}
BAD_FILES = {'hello.py': b'print("hi")\n', '../escape.py': b'nope\n'}
CONFLICT_FILES = {'pkg': b'plain file\n', 'pkg/module.py': b'child module\n'}
WRITE_TARGET = 'netbox_custom_scripts.storage.service.store.write_staged_revision'


def content(marker):
    """Return a distinct source tree, so each revision gets its own digest."""
    return {'hello.py': f'VALUE = {marker}\n'.encode()}


class ShiftingFiles(Mapping):
    """A source mapping whose content differs on every read, as a live view could."""

    def __init__(self):
        self.version = 0

    def __iter__(self):
        self.version += 1
        return iter(('hello.py',))

    def __getitem__(self, key):
        return f'VERSION = {self.version}\n'.encode()

    def __len__(self):
        return 1


class StorageServiceMixin:
    def setUp(self):
        super().setUp()
        self.root = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'project_root': self.root}}))
        self.project = CustomScriptProject.objects.create(name='Staged Project', key='staged-project')

    def project_path(self, project=None):
        return project_directory(self.root, (project or self.project).storage_key)

    def revision_path(self, digest, project=None):
        return revision_directory(self.root, (project or self.project).storage_key, digest)

    def materialize(self, files=None, project=None):
        """Stage content the way a caller would, leaving the revision MATERIALIZED."""
        revision, _ = service.stage_revision(project or self.project, files or GOOD_FILES)
        return revision

    def validated(self, files=None, project=None):
        """Materialize content, then promote it the way project validation eventually will."""
        revision = self.materialize(files, project)
        revision.status = RevisionStatusChoices.VALID
        revision.save()
        return revision


class StageRevisionTestCase(StorageServiceMixin, TestCase):
    def test_stage_revision_materializes_safe_content(self):
        revision, created = service.stage_revision(self.project, GOOD_FILES)
        self.assertTrue(created)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertRegex(revision.digest, r'^[0-9a-f]{64}$')
        self.assertEqual(revision.file_count, 2)
        self.assertEqual(revision.total_size, sum(len(body) for body in GOOD_FILES.values()))
        self.assertEqual(revision.validation_errors, [])
        self.assertEqual([entry['path'] for entry in revision.manifest], ['hello.py', 'pkg/mod.py'])

    def test_stage_revision_never_reports_valid(self):
        # VALID means the project passed import and discovery validation, which the storage
        # layer does not perform, so it must never hand out that status.
        revision = self.materialize()
        self.assertNotEqual(revision.status, RevisionStatusChoices.VALID)

    def test_stage_revision_writes_the_revision_directory(self):
        revision = self.materialize()
        stored = self.revision_path(revision.digest)
        self.assertEqual((stored / 'hello.py').read_bytes(), GOOD_FILES['hello.py'])
        self.assertEqual((stored / 'pkg' / 'mod.py').read_bytes(), GOOD_FILES['pkg/mod.py'])

    def test_stage_revision_returns_existing_revision_for_identical_content(self):
        first, first_created = service.stage_revision(self.project, GOOD_FILES)
        second, second_created = service.stage_revision(self.project, GOOD_FILES)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(self.project.revisions.count(), 1)

    def test_stage_revision_verifies_the_stored_tree_before_reusing_it(self):
        revision = self.materialize()
        # Same length as the original, so only the checksum can catch the swap.
        tampered = b'print("no")\n'
        self.assertEqual(len(tampered), len(GOOD_FILES['hello.py']))
        (self.revision_path(revision.digest) / 'hello.py').write_bytes(tampered)
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.stage_revision(self.project, GOOD_FILES)
        self.assertIn('checksum_mismatch:hello.py', ctx.exception.reasons)

    def test_stage_revision_creates_invalid_revision_preserving_errors_and_partial_manifest(self):
        revision, created = service.stage_revision(self.project, BAD_FILES)
        self.assertTrue(created)
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertIsNone(revision.digest)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['hello.py'])
        self.assertEqual(revision.file_count, 1)
        self.assertEqual(len(revision.validation_errors), 1)
        self.assertEqual(revision.validation_errors[0]['code'], 'path_traversal')

    def test_stage_revision_writes_nothing_to_disk_for_invalid_content(self):
        service.stage_revision(self.project, BAD_FILES)
        self.assertFalse(self.project_path().exists())
        self.assertEqual(list(pathlib.Path(self.root).iterdir()), [])

    def test_stage_revision_rejects_a_file_that_is_also_a_directory(self):
        revision, created = service.stage_revision(self.project, CONFLICT_FILES)
        self.assertTrue(created)
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertIsNone(revision.digest)
        codes = [error['code'] for error in revision.validation_errors]
        self.assertEqual(codes, ['path_conflict'])
        self.assertEqual([entry['path'] for entry in revision.manifest], ['pkg'])
        self.assertFalse(self.project_path().exists())

    def test_stage_revision_creates_a_distinct_invalid_revision_each_time(self):
        first, _ = service.stage_revision(self.project, BAD_FILES)
        second, second_created = service.stage_revision(self.project, BAD_FILES)
        self.assertTrue(second_created)
        self.assertNotEqual(first.pk, second.pk)
        self.assertIsNone(first.digest)
        self.assertIsNone(second.digest)

    def test_stage_revision_records_storage_failed_and_reraises_on_write_failure(self):
        with (
            mock.patch(WRITE_TARGET, side_effect=OSError('no space left on device')),
            self.assertRaises(OSError),
        ):
            service.stage_revision(self.project, GOOD_FILES)
        revision = self.project.revisions.get()
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        self.assertIsNotNone(revision.digest)
        self.assertEqual(revision.validation_errors[0]['code'], 'storage_write_failed')

    def test_stage_revision_records_a_tree_that_could_not_be_removed(self):
        # The store converts a tree it installed, failed to verify, and could not remove into a
        # message naming what became of it. This is where that message has to arrive for an
        # operator or a later reconciler to act on it, and the revision must not be materialized.
        corrupt = RevisionCorruptError('The stored revision does not match its manifest.', ['size_mismatch:hello.py'])
        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=corrupt),
            mock.patch.object(store.shutil, 'rmtree', side_effect=PermissionError('denied')),
            self.assertLogs(store.logger, 'ERROR'),
            self.assertRaises(StorageError),
        ):
            service.stage_revision(self.project, GOOD_FILES)

        revision = self.project.revisions.get()
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        self.assertEqual(revision.validation_errors[0]['code'], 'storage_write_failed')
        message = revision.validation_errors[0]['message']
        self.assertIn('does not match its manifest', message)
        self.assertIn('set aside', message)

    def test_stage_revision_retries_a_revision_whose_write_failed(self):
        with (
            mock.patch(WRITE_TARGET, side_effect=OSError('no space left on device')),
            self.assertRaises(OSError),
        ):
            service.stage_revision(self.project, GOOD_FILES)
        failed = self.project.revisions.get()

        revision, created = service.stage_revision(self.project, GOOD_FILES)
        self.assertFalse(created)
        self.assertEqual(revision.pk, failed.pk)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(revision.validation_errors, [])
        self.assertTrue(self.revision_path(revision.digest).is_dir())

    def test_stage_revision_does_not_resurrect_a_rejected_revision(self):
        # Once project validation rejects a revision, re-staging the same bytes must not
        # clear its errors or promote it. Only the validator may move it out of INVALID.
        revision = self.materialize()
        revision.status = RevisionStatusChoices.INVALID
        revision.validation_errors = [{'path': 'hello.py', 'code': 'syntax_error', 'message': 'bad syntax'}]
        revision.save()

        again, created = service.stage_revision(self.project, GOOD_FILES)
        self.assertFalse(created)
        self.assertEqual(again.pk, revision.pk)
        self.assertEqual(again.status, RevisionStatusChoices.INVALID)
        self.assertEqual(again.validation_errors[0]['code'], 'syntax_error')

    def test_stage_revision_ignores_a_mutated_in_memory_storage_key(self):
        # The caller's instance contributes only its primary key.
        stale = CustomScriptProject.objects.get(pk=self.project.pk)
        stale.storage_key = CustomScriptProject.objects.create(name='Other', key='other').storage_key
        revision = self.materialize(project=stale)
        self.assertTrue(self.revision_path(revision.digest).is_dir())

    def test_stage_revision_does_not_re_drive_a_revision_under_validation(self):
        # VALIDATING belongs to project validation. Re-staging identical content while the
        # validator holds a revision must not rewrite its tree or reset its status.
        revision = self.materialize()
        revision.status = RevisionStatusChoices.VALIDATING
        revision.validation_errors = [{'path': None, 'code': 'in_progress', 'message': 'validating'}]
        revision.save()

        again, created = service.stage_revision(self.project, GOOD_FILES)

        self.assertFalse(created)
        self.assertEqual(again.pk, revision.pk)
        self.assertEqual(again.status, RevisionStatusChoices.VALIDATING)
        self.assertEqual(again.validation_errors[0]['code'], 'in_progress')

    def test_stage_revision_snapshots_the_source_mapping(self):
        # The manifest and the write must describe one set of bytes, so the mapping is read
        # once. A mapping that answers differently per read would otherwise store content its
        # manifest does not describe.
        revision, _ = service.stage_revision(self.project, ShiftingFiles())
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        stored = (self.revision_path(revision.digest) / 'hello.py').read_bytes()
        self.assertEqual(stored, b'VERSION = 1\n')
        self.assertEqual(revision.manifest[0]['size'], len(stored))

    def test_stage_revision_records_storage_failed_when_an_existing_tree_is_corrupt(self):
        # A retry whose destination already exists but does not match must not strand the row
        # mid-write: verification raises a StorageError, not an OSError.
        with (
            mock.patch(WRITE_TARGET, side_effect=OSError('no space left on device')),
            self.assertRaises(OSError),
        ):
            service.stage_revision(self.project, GOOD_FILES)
        failed = self.project.revisions.get()
        stored = self.revision_path(failed.digest)
        stored.mkdir(parents=True)
        (stored / 'hello.py').write_bytes(b'wrong content\n')

        with self.assertRaises(RevisionCorruptError):
            service.stage_revision(self.project, GOOD_FILES)

        failed.refresh_from_db()
        self.assertEqual(failed.status, RevisionStatusChoices.STORAGE_FAILED)
        self.assertEqual(failed.validation_errors[0]['code'], 'storage_write_failed')

    def test_stage_revision_rejects_a_row_whose_counters_disagree_with_its_manifest(self):
        # The counters summarize the manifest, so a row where they disagree describes no tree
        # anyone can verify. QuerySet.update bypasses the immutability guard, which is the
        # documented way these fields can drift.
        cases = (('file_count', 99, 'file_count_mismatch'), ('total_size', 999_999, 'total_size_mismatch'))
        for marker, (field, value, reason) in enumerate(cases):
            with self.subTest(field=field):
                # Distinct content per case, so each lands on its own row rather than
                # colliding on one digest.
                files = content(marker + 40)
                revision = self.materialize(files)
                CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(**{field: value})
                with self.assertRaises(RevisionCorruptError) as ctx:
                    service.stage_revision(self.project, files)
                self.assertIn(reason, ctx.exception.reasons)

    def test_stage_revision_uses_the_alias_the_project_came_from(self):
        # Every read and write of one staging operation lands on a single connection. A
        # project carrying a foreign alias proves the value comes from the instance rather
        # than being re-resolved, and that nothing quietly falls back to the default.
        self.project._state.db = 'schema_example'
        with self.assertRaises(ConnectionDoesNotExist):
            service.stage_revision(self.project, GOOD_FILES)
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 0)

    def test_stage_revision_rejects_a_manifest_that_leaves_the_revision_directory(self):
        revision = self.materialize()
        poisoned = [{'path': '../escape.py', 'size': 1, 'sha256': 'a' * 64}]
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(manifest=poisoned, file_count=1, total_size=1)
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.stage_revision(self.project, GOOD_FILES)
        self.assertEqual(ctx.exception.reasons, ('path_traversal:../escape.py',))


class BranchingGuardTestCase(StorageServiceMixin, TestCase):
    """
    Refuse the operations that move source while branching may not keep these models in main.

    What is under test here is the guard's placement, so the routing answer is stubbed. Whether
    the answer itself is right is settled in tests/test_branching.py.
    """

    def unsafe(self):
        return mock.patch.object(service.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.')

    def test_staging_is_refused_and_creates_no_revision(self):
        # The guard runs before anything is written, so there is no half-staged row to explain.
        with self.unsafe(), self.assertRaises(ImproperlyConfigured):
            service.stage_revision(self.project, GOOD_FILES)
        self.assertFalse(self.project.revisions.exists())
        self.assertFalse(self.project_path().exists())

    def test_activation_is_refused(self):
        revision = self.validated()
        with self.unsafe(), self.assertRaises(ImproperlyConfigured):
            service.activate_revision(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_operations_are_permitted_when_routing_is_safe(self):
        # The ordinary case, asserted so the guard cannot quietly refuse everything.
        revision, _ = service.stage_revision(self.project, GOOD_FILES)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)


class ActivateRevisionTestCase(StorageServiceMixin, TestCase):
    def test_activate_revision_promotes_validated_revision(self):
        revision = self.validated()
        activated = service.activate_revision(revision)
        self.project.refresh_from_db()
        self.assertEqual(activated.status, RevisionStatusChoices.ACTIVE)
        self.assertIsNotNone(activated.activated)
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_activate_revision_rejects_a_merely_materialized_revision(self):
        revision = self.materialize()
        with self.assertRaises(ActivationError):
            service.activate_revision(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activate_revision_retires_previous_active_revision(self):
        first = self.validated(content(1))
        second = self.validated(content(2))
        service.activate_revision(first)
        service.activate_revision(second)
        first.refresh_from_db()
        second.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, second.pk)

    def test_activate_revision_reactivates_retired_revision(self):
        first = self.validated(content(1))
        second = self.validated(content(2))
        service.activate_revision(first)
        service.activate_revision(second)
        service.activate_revision(first)
        first.refresh_from_db()
        second.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(second.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(self.project.active_revision_id, first.pk)

    def test_activate_revision_is_idempotent_on_already_active_revision(self):
        revision = self.validated()
        first = service.activate_revision(revision)
        second = service.activate_revision(revision)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(first.activated, second.activated)

    def test_activate_revision_rejects_rejected_revision(self):
        revision = self.materialize()
        revision.status = RevisionStatusChoices.INVALID
        revision.save()
        with self.assertRaises(ActivationError):
            service.activate_revision(revision)

    def test_activate_revision_rejects_staging_or_validating_revision(self):
        for marker, status in enumerate((RevisionStatusChoices.STAGING, RevisionStatusChoices.VALIDATING)):
            with self.subTest(status=status):
                revision = self.materialize(content(marker + 10))
                revision.status = status
                revision.save()
                with self.assertRaises(ActivationError):
                    service.activate_revision(revision)

    def test_a_digestless_activatable_revision_cannot_exist(self):
        # activate_revision still guards against a null digest, but the check constraint now
        # makes that row impossible to create, so this covers the invariant at its source.
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProjectRevision.objects.create(
                project=self.project, digest=None, status=RevisionStatusChoices.VALID
            )

    def test_activate_revision_fails_when_the_stored_tree_is_missing(self):
        revision = self.validated()
        (self.revision_path(revision.digest) / 'hello.py').unlink()
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.activate_revision(revision)
        self.assertIn('missing_or_special:hello.py', ctx.exception.reasons)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activate_revision_fails_when_a_stored_file_was_modified(self):
        revision = self.validated()
        (self.revision_path(revision.digest) / 'hello.py').write_bytes(b'tampered with\n')
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.activate_revision(revision)
        self.assertTrue(
            any(reason.startswith(('size_mismatch', 'checksum_mismatch')) for reason in ctx.exception.reasons)
        )

    def test_activate_revision_ignores_a_mutated_in_memory_project(self):
        # The owning project is read from the database, so pointing the in-memory instance at
        # another project cannot create a cross-project active revision.
        other = CustomScriptProject.objects.create(name='Other Project', key='other-project')
        revision = self.validated()
        revision.project = other

        service.activate_revision(revision)

        self.project.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertIsNone(other.active_revision_id)

    def test_activate_revision_locks_project_before_revision(self):
        revision = self.validated()
        with CaptureQueriesContext(connection) as queries:
            service.activate_revision(revision)
        locking = [entry['sql'] for entry in queries if 'FOR UPDATE' in entry['sql']]
        self.assertEqual(len(locking), 2)
        self.assertIn('"netbox_custom_scripts_customscriptproject"', locking[0])
        self.assertIn('"netbox_custom_scripts_customscriptprojectrevision"', locking[1])

    def test_activate_revision_uses_the_alias_the_revision_came_from(self):
        # A revision loaded from one connection must not be activated against another, so the
        # instance's own alias wins over anything a router would pick per query.
        revision = self.validated()
        revision._state.db = 'schema_example'
        with self.assertRaises(ConnectionDoesNotExist):
            service.activate_revision(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activate_revision_falls_back_to_the_router_without_an_alias(self):
        # The transaction and the row locks have to sit on one connection, so an instance
        # with no alias of its own still resolves exactly one.
        revision = self.validated()
        revision._state.db = None
        with mock.patch.object(router, 'db_for_write', return_value=DEFAULT_DB_ALIAS) as db_for_write:
            activated = service.activate_revision(revision)
        self.assertEqual(activated.status, RevisionStatusChoices.ACTIVE)
        self.assertIn(CustomScriptProjectRevision, [call.args[0] for call in db_for_write.call_args_list])


class SourceMappingContractTestCase(StorageServiceMixin, TestCase):
    """
    Cover the mapping contract, which is what keeps the manifest describing what was written.

    A value that can change between hashing and writing produces a tree the store refuses and
    withdraws, so the snapshot has to be a copy of the bytes and not just of the mapping.
    """

    def test_a_mutated_bytearray_cannot_change_what_is_written(self):
        content = bytearray(b'original')
        revision = self.materialize({'hello.py': content})
        content[:] = b'replaced'
        self.assertEqual((self.revision_path(revision.digest) / 'hello.py').read_bytes(), b'original')
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)

    def test_a_string_value_is_a_caller_fault_not_an_invalid_revision(self):
        with self.assertRaises(TypeError) as ctx:
            service.stage_revision(self.project, {'hello.py': 'not bytes'})
        self.assertIn('hello.py', str(ctx.exception))
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 0)

    def test_a_non_string_key_is_rejected(self):
        with self.assertRaises(TypeError):
            service.stage_revision(self.project, {0: b'x'})
        self.assertEqual(CustomScriptProjectRevision.objects.count(), 0)

    def test_a_memoryview_value_is_accepted_and_copied(self):
        buffer = bytearray(b'viewed')
        revision = self.materialize({'hello.py': memoryview(buffer)})
        self.assertEqual((self.revision_path(revision.digest) / 'hello.py').read_bytes(), b'viewed')
