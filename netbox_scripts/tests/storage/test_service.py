from collections.abc import Mapping
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.core.files.base import ContentFile
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, router, transaction
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, service, store
from netbox_scripts.storage.exceptions import (
    ActivationError,
    ProjectVanishedError,
    RevisionCorruptError,
    StorageError,
)
from netbox_scripts.storage.paths import STORAGE_PREFIX, project_prefix, revision_prefix
from netbox_scripts.storage.script_files import EMPTY_SNAPSHOT_DIGEST, build_script_file_snapshot
from netbox_scripts.tests.storage.test_store import RefusingStorage

GOOD_FILES = {'hello.py': b'print("hi")\n', 'pkg/mod.py': b'VALUE = 1\n'}
BAD_FILES = {'hello.py': b'print("hi")\n', '../escape.py': b'nope\n'}
CONFLICT_FILES = {'pkg': b'plain file\n', 'pkg/module.py': b'child module\n'}
WRITE_TARGET = 'netbox_scripts.storage.service.store.write_revision'


def promote_without_synchronizing(**kwargs):
    """Stand-in for the mandatory on_promote callback, for cases testing the primitive alone."""


IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


def content(marker):
    """Return a distinct source tree, so each revision gets its own digest."""
    return {'hello.py': f'VALUE = {marker}\n'.encode()}


def stored_paths(storage, prefix):
    """
    Return every path actually stored under one prefix, relative to it.

    Tests enumerate the backend to pin exactly what an operation left behind. Production code
    works from the manifest and never lists, so this walk lives here.
    """
    found = set()
    pending = ['']
    while pending:
        relative = pending.pop()
        try:
            directories, names = storage.listdir(f'{prefix}{relative}')
        except FileNotFoundError:
            continue
        found.update(f'{relative}{name}' for name in names)
        pending.extend(f'{relative}{name}/' for name in directories)
    return found


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
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.storage = config.get_storage()
        self.project = ScriptProject.objects.create(name='Staged Project', key='staged-project')

    def project_keys(self, project=None):
        """Return every key one project holds, relative to its prefix."""
        return stored_paths(self.storage, project_prefix((project or self.project).storage_key))

    def revision_keys(self, digest, project=None):
        """Return every key one revision holds, relative to its prefix."""
        return stored_paths(self.storage, revision_prefix((project or self.project).storage_key, digest))

    def read(self, digest, path, project=None):
        """Return the bytes stored for one file of a revision."""
        key = f'{revision_prefix((project or self.project).storage_key, digest)}{path}'
        with self.storage.open(key, 'rb') as handle:
            return handle.read()

    def overwrite(self, digest, path, content, project=None):
        """Replace one stored file, standing in for content tampered with after a write."""
        key = f'{revision_prefix((project or self.project).storage_key, digest)}{path}'
        self.storage.delete(key)
        self.storage.save(key, ContentFile(content))

    def remove(self, digest, path, project=None):
        """Remove one stored file of a revision."""
        self.storage.delete(f'{revision_prefix((project or self.project).storage_key, digest)}{path}')

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

    def test_stage_revision_stores_the_revision_content(self):
        revision = self.materialize()
        self.assertEqual(self.read(revision.digest, 'hello.py'), GOOD_FILES['hello.py'])
        self.assertEqual(self.read(revision.digest, 'pkg/mod.py'), GOOD_FILES['pkg/mod.py'])

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
        self.overwrite(revision.digest, 'hello.py', tampered)
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

    def test_stage_revision_stores_nothing_for_invalid_content(self):
        service.stage_revision(self.project, BAD_FILES)
        self.assertEqual(self.project_keys(), set())
        self.assertEqual(stored_paths(self.storage, f'{STORAGE_PREFIX}/'), set())

    def test_stage_revision_rejects_a_file_that_is_also_a_directory(self):
        revision, created = service.stage_revision(self.project, CONFLICT_FILES)
        self.assertTrue(created)
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertIsNone(revision.digest)
        codes = [error['code'] for error in revision.validation_errors]
        self.assertEqual(codes, ['path_conflict'])
        self.assertEqual([entry['path'] for entry in revision.manifest], ['pkg'])
        self.assertEqual(self.project_keys(), set())

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

    def test_stage_revision_records_a_tree_that_failed_verification(self):
        # This is where a verification failure has to arrive for an operator or a later
        # reconciler to act on. The revision must not be materialized, and the written keys
        # stay for the retry that owns them.
        corrupt = RevisionCorruptError('The stored revision does not match its manifest.', ['size_mismatch:hello.py'])
        with (
            mock.patch.object(store, '_verify_tree', side_effect=corrupt),
            self.assertRaises(StorageError),
        ):
            service.stage_revision(self.project, GOOD_FILES)

        revision = self.project.revisions.get()
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        self.assertEqual(revision.validation_errors[0]['code'], 'storage_write_failed')
        self.assertIn('does not match its manifest', revision.validation_errors[0]['message'])
        self.assertEqual(self.revision_keys(revision.digest), set(GOOD_FILES))

    def test_stage_revision_records_storage_failed_when_the_backend_cannot_answer_exists(self):
        # exists() is the first backend call a write makes, and a cloud backend can raise
        # something there that is neither OSError nor StorageError. The row must land in
        # STORAGE_FAILED rather than staying in STAGING with nothing recorded.
        refusing = RefusingStorage(failing={'exists'}, error=RuntimeError('the sdk gave up'))
        with (
            mock.patch('netbox_scripts.storage.service.config.get_storage', return_value=refusing),
            self.assertRaises(StorageError),
        ):
            service.stage_revision(self.project, GOOD_FILES)

        revision = self.project.revisions.get()
        self.assertEqual(revision.status, RevisionStatusChoices.STORAGE_FAILED)
        self.assertEqual(revision.validation_errors[0]['code'], 'storage_write_failed')

    def test_a_slow_stager_does_not_demote_a_concurrently_promoted_revision(self):
        # Project validation can promote the row while a duplicate stager is still writing
        # bytes. Promotion belongs to that owner, so the stager returns the row as found.
        def promote(*args, **kwargs):
            ScriptProjectRevision.objects.filter(project=self.project).update(status=RevisionStatusChoices.VALID)

        with mock.patch(WRITE_TARGET, side_effect=promote):
            revision, created = service.stage_revision(self.project, GOOD_FILES)

        self.assertTrue(created)
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_a_slow_failed_stager_does_not_demote_a_concurrently_promoted_revision(self):
        # Only STAGING may become STORAGE_FAILED. A row that advanced while this writer was
        # failing keeps the concurrent owner's word, and the error still reaches the caller.
        def promote_then_fail(*args, **kwargs):
            ScriptProjectRevision.objects.filter(project=self.project).update(status=RevisionStatusChoices.VALID)
            raise OSError('no space left on device')

        with mock.patch(WRITE_TARGET, side_effect=promote_then_fail), self.assertRaises(OSError):
            service.stage_revision(self.project, GOOD_FILES)

        revision = self.project.revisions.get()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertEqual(revision.validation_errors, [])

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
        self.assertEqual(self.revision_keys(revision.digest), {'hello.py', 'pkg/mod.py'})

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
        stale = ScriptProject.objects.get(pk=self.project.pk)
        stale.storage_key = ScriptProject.objects.create(name='Other', key='other').storage_key
        revision = self.materialize(project=stale)
        self.assertEqual(self.revision_keys(revision.digest), {'hello.py', 'pkg/mod.py'})

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
        stored = self.read(revision.digest, 'hello.py')
        self.assertEqual(stored, b'VERSION = 1\n')
        self.assertEqual(revision.manifest[0]['size'], len(stored))

    def test_stage_revision_repairs_a_partial_tree_left_by_a_failed_write(self):
        # A write that died partway leaves keys behind under the digest. The retry owns them:
        # the digest already fixed what the content must be, so whatever disagrees is replaced.
        with (
            mock.patch(WRITE_TARGET, side_effect=OSError('no space left on device')),
            self.assertRaises(OSError),
        ):
            service.stage_revision(self.project, GOOD_FILES)
        failed = self.project.revisions.get()
        self.overwrite(failed.digest, 'hello.py', b'wrong content\n')

        revision, created = service.stage_revision(self.project, GOOD_FILES)

        self.assertFalse(created)
        self.assertEqual(revision.pk, failed.pk)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(self.read(revision.digest, 'hello.py'), GOOD_FILES['hello.py'])
        self.assertEqual(self.revision_keys(revision.digest), {'hello.py', 'pkg/mod.py'})
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
                ScriptProjectRevision.objects.filter(pk=revision.pk).update(**{field: value})
                with self.assertRaises(RevisionCorruptError) as ctx:
                    service.stage_revision(self.project, files)
                self.assertIn(reason, ctx.exception.reasons)

    def test_stage_revision_refuses_a_project_from_another_database(self):
        # The lifecycle contract is enforced up front. Content staged on another alias
        # could never record its deletion cleanup, so nothing is written anywhere.
        self.project._state.db = 'schema_example'
        with self.assertRaises(ImproperlyConfigured) as ctx:
            service.stage_revision(self.project, GOOD_FILES)
        self.assertIn('"default"', str(ctx.exception))
        self.assertEqual(ScriptProjectRevision.objects.count(), 0)
        self.assertEqual(self.project_keys(), set())

    def test_stage_revision_rejects_a_manifest_that_leaves_the_revision_directory(self):
        revision = self.materialize()
        poisoned = [{'path': '../escape.py', 'size': 1, 'sha256': 'a' * 64}]
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(manifest=poisoned, file_count=1, total_size=1)
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
        self.assertEqual(self.project_keys(), set())

    def test_activation_is_refused(self):
        revision = self.validated()
        with self.unsafe(), self.assertRaises(ImproperlyConfigured):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_operations_are_permitted_when_routing_is_safe(self):
        # The ordinary case, asserted so the guard cannot quietly refuse everything.
        revision, _ = service.stage_revision(self.project, GOOD_FILES)
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)


class ActivateRevisionTestCase(StorageServiceMixin, TestCase):
    def test_promote_revision_promotes_validated_revision(self):
        revision = self.validated()
        activated = service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.project.refresh_from_db()
        self.assertEqual(activated.status, RevisionStatusChoices.ACTIVE)
        self.assertIsNotNone(activated.activated)
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_a_project_deleted_before_the_promote_lock_reports_the_vanished_project(self):
        revision = self.validated()

        def delete_the_project(*args, **kwargs):
            ScriptProject.objects.filter(pk=revision.project_id).delete()

        # Verification runs after the unlocked reads and before the row lock, which is the
        # window a concurrent delete lands in.
        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=delete_the_project),
            self.assertRaises(ProjectVanishedError),
        ):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)

    def test_promote_revision_rejects_a_merely_materialized_revision(self):
        revision = self.materialize()
        with self.assertRaises(ActivationError):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_promote_revision_retires_previous_active_revision(self):
        first = self.validated(content(1))
        second = self.validated(content(2))
        service.promote_revision(first, on_promote=promote_without_synchronizing)
        service.promote_revision(second, on_promote=promote_without_synchronizing)
        first.refresh_from_db()
        second.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, second.pk)

    def test_promote_revision_reactivates_retired_revision(self):
        first = self.validated(content(1))
        second = self.validated(content(2))
        service.promote_revision(first, on_promote=promote_without_synchronizing)
        service.promote_revision(second, on_promote=promote_without_synchronizing)
        service.promote_revision(first, on_promote=promote_without_synchronizing)
        first.refresh_from_db()
        second.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(second.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(self.project.active_revision_id, first.pk)

    def test_promote_revision_is_idempotent_on_already_active_revision(self):
        revision = self.validated()
        first = service.promote_revision(revision, on_promote=promote_without_synchronizing)
        second = service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(first.activated, second.activated)

    def test_promote_revision_rejects_rejected_revision(self):
        revision = self.materialize()
        revision.status = RevisionStatusChoices.INVALID
        revision.save()
        with self.assertRaises(ActivationError):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)

    def test_promote_revision_rejects_staging_or_validating_revision(self):
        for marker, status in enumerate((RevisionStatusChoices.STAGING, RevisionStatusChoices.VALIDATING)):
            with self.subTest(status=status):
                revision = self.materialize(content(marker + 10))
                revision.status = status
                revision.save()
                with self.assertRaises(ActivationError):
                    service.promote_revision(revision, on_promote=promote_without_synchronizing)

    def test_a_digestless_activatable_revision_cannot_exist(self):
        # promote_revision still guards against a null digest, but the check constraint now
        # makes that row impossible to create, so this covers the invariant at its source.
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScriptProjectRevision.objects.create(project=self.project, digest=None, status=RevisionStatusChoices.VALID)

    def test_promote_revision_fails_when_the_stored_tree_is_missing(self):
        revision = self.validated()
        self.remove(revision.digest, 'hello.py')
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertIn('missing:hello.py', ctx.exception.reasons)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_promote_revision_fails_when_a_stored_file_was_modified(self):
        revision = self.validated()
        self.overwrite(revision.digest, 'hello.py', b'tampered with\n')
        with self.assertRaises(RevisionCorruptError) as ctx:
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertTrue(
            any(reason.startswith(('size_mismatch', 'checksum_mismatch')) for reason in ctx.exception.reasons)
        )

    def test_promote_revision_ignores_a_mutated_in_memory_project(self):
        # The owning project is read from the database, so pointing the in-memory instance at
        # another project cannot create a cross-project active revision.
        other = ScriptProject.objects.create(name='Other Project', key='other-project')
        revision = self.validated()
        revision.project = other

        service.promote_revision(revision, on_promote=promote_without_synchronizing)

        self.project.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertIsNone(other.active_revision_id)

    def test_promote_revision_locks_project_before_revision(self):
        revision = self.validated()
        with CaptureQueriesContext(connection) as queries:
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        locking = [entry['sql'] for entry in queries if 'FOR UPDATE' in entry['sql']]
        self.assertEqual(len(locking), 2)
        self.assertIn('"netbox_scripts_scriptproject"', locking[0])
        self.assertIn('"netbox_scripts_scriptprojectrevision"', locking[1])

    def test_promote_revision_refuses_a_revision_from_another_database(self):
        # Same contract as staging. A revision loaded from another alias is refused
        # before any read or lock, so the row and the pointer stay untouched.
        revision = self.validated()
        revision._state.db = 'schema_example'
        with self.assertRaises(ImproperlyConfigured) as ctx:
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertIn('"default"', str(ctx.exception))
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)
        fresh = ScriptProjectRevision.objects.get(pk=revision.pk)
        self.assertEqual(fresh.status, RevisionStatusChoices.VALID)

    def test_promote_revision_falls_back_to_the_router_without_an_alias(self):
        # The transaction and the row locks have to sit on one connection, so an instance
        # with no alias of its own still resolves exactly one.
        revision = self.validated()
        revision._state.db = None
        with mock.patch.object(router, 'db_for_write', return_value=DEFAULT_DB_ALIAS) as db_for_write:
            activated = service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertEqual(activated.status, RevisionStatusChoices.ACTIVE)
        self.assertIn(ScriptProjectRevision, [call.args[0] for call in db_for_write.call_args_list])

    def test_activation_verifies_content_before_taking_the_row_locks(self):
        # Verification downloads and hashes every file, so it must not run inside the
        # transaction. Savepoint depth is the observable: entering the service transaction
        # inside a TestCase pushes one more savepoint, verification must see the baseline.
        revision = self.validated()
        real_verify = store.verify_revision_tree
        depths = []

        def recording_verify(*args, **kwargs):
            depths.append(len(connection.savepoint_ids))
            return real_verify(*args, **kwargs)

        baseline = len(connection.savepoint_ids)
        with mock.patch.object(store, 'verify_revision_tree', side_effect=recording_verify):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertEqual(depths, [baseline])

    def test_activation_rejects_a_revision_that_changed_after_verification(self):
        # The verified snapshot is compared against the locked row, so content swapped in
        # the unlocked window is refused rather than promoted on stale evidence.
        revision = self.validated()
        real_verify = store.verify_revision_tree

        def verify_then_swap(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            ScriptProjectRevision.objects.filter(pk=revision.pk).update(digest='0' * 64)
            return result

        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=verify_then_swap),
            self.assertRaises(ActivationError) as ctx,
        ):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertIn('changed while', str(ctx.exception))
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)


class SourceMappingContractTestCase(StorageServiceMixin, TestCase):
    """
    Cover the mapping contract, which is what keeps the manifest describing what was written.

    A value that can change between hashing and writing produces a tree the store refuses,
    so the snapshot has to be a copy of the bytes and not just of the mapping.
    """

    def test_a_mutated_bytearray_cannot_change_what_is_written(self):
        content = bytearray(b'original')
        revision = self.materialize({'hello.py': content})
        content[:] = b'replaced'
        self.assertEqual(self.read(revision.digest, 'hello.py'), b'original')
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)

    def test_a_string_value_is_a_caller_fault_not_an_invalid_revision(self):
        with self.assertRaises(TypeError) as ctx:
            service.stage_revision(self.project, {'hello.py': 'not bytes'})
        self.assertIn('hello.py', str(ctx.exception))
        self.assertEqual(ScriptProjectRevision.objects.count(), 0)

    def test_a_non_string_key_is_rejected(self):
        with self.assertRaises(TypeError):
            service.stage_revision(self.project, {0: b'x'})
        self.assertEqual(ScriptProjectRevision.objects.count(), 0)

    def test_a_memoryview_value_is_accepted_and_copied(self):
        buffer = bytearray(b'viewed')
        revision = self.materialize({'hello.py': memoryview(buffer)})
        self.assertEqual(self.read(revision.digest, 'hello.py'), b'viewed')


class ScriptFileIdentityTestCase(StorageServiceMixin, TestCase):
    """Staging freezes the enabled Module declarations into the revision's identity."""

    def script_file(self, path, **kwargs):
        return ScriptFile.objects.create(project=self.project, source_path=path, **kwargs)

    def test_stage_revision_freezes_the_enabled_declarations(self):
        enabled = self.script_file('hello.py')
        self.script_file('parked.py', enabled=False)
        revision = self.materialize()
        self.assertEqual(revision.script_file_snapshot, [{'script_file': enabled.pk, 'source_path': 'hello.py'}])
        _, expected_digest = build_script_file_snapshot([enabled])
        self.assertEqual(revision.script_file_digest, expected_digest)

    def test_staging_without_script_files_carries_the_empty_snapshot(self):
        revision = self.materialize()
        self.assertEqual(revision.script_file_snapshot, [])
        self.assertEqual(revision.script_file_digest, EMPTY_SNAPSHOT_DIGEST)

    def test_identical_content_under_a_changed_configuration_is_a_new_revision(self):
        first = self.materialize()
        self.script_file('hello.py')
        second, created = service.stage_revision(self.project, GOOD_FILES)
        self.assertTrue(created)
        self.assertNotEqual(second.pk, first.pk)
        self.assertEqual(second.digest, first.digest)
        self.assertEqual(second.status, RevisionStatusChoices.MATERIALIZED)

    def test_identical_content_and_configuration_returns_the_existing_revision(self):
        self.script_file('hello.py')
        first, first_created = service.stage_revision(self.project, GOOD_FILES)
        second, second_created = service.stage_revision(self.project, GOOD_FILES)
        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second.pk, first.pk)

    def test_an_invalid_staging_attempt_still_freezes_the_configuration(self):
        script_file = self.script_file('hello.py')
        revision, _ = service.stage_revision(self.project, BAD_FILES)
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertEqual(revision.script_file_snapshot, [{'script_file': script_file.pk, 'source_path': 'hello.py'}])


class RefreshScriptFilesTestCase(StorageServiceMixin, TestCase):
    """refresh_revision_entrypoints restages stored content under the current configuration."""

    def test_refresh_stages_the_content_under_the_current_configuration(self):
        first = self.materialize()
        script_file = ScriptFile.objects.create(project=self.project, source_path='hello.py')
        refreshed, created = service.refresh_revision_script_files(first)
        self.assertTrue(created)
        self.assertNotEqual(refreshed.pk, first.pk)
        self.assertEqual(refreshed.digest, first.digest)
        self.assertEqual(refreshed.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(refreshed.manifest, first.manifest)
        self.assertEqual(refreshed.script_file_snapshot, [{'script_file': script_file.pk, 'source_path': 'hello.py'}])

    def test_refresh_returns_the_existing_row_for_an_unchanged_configuration(self):
        first = self.materialize()
        again, created = service.refresh_revision_script_files(first)
        self.assertFalse(created)
        self.assertEqual(again.pk, first.pk)

    def test_refresh_refuses_a_revision_without_content(self):
        revision, _ = service.stage_revision(self.project, BAD_FILES)
        with self.assertRaises(ValueError):
            service.refresh_revision_script_files(revision)

    def test_refresh_verifies_the_stored_tree(self):
        first = self.materialize()
        ScriptFile.objects.create(project=self.project, source_path='hello.py')
        self.overwrite(first.digest, 'hello.py', b'tampered')
        with self.assertRaises(RevisionCorruptError):
            service.refresh_revision_script_files(first)

    def test_refresh_leaves_the_source_verdict_untouched(self):
        # An INVALID verdict binds to the old configuration. The fix-a-typo path creates a
        # fresh candidate for the same content rather than resurrecting the judged row.
        first = self.materialize()
        ScriptProjectRevision.objects.filter(pk=first.pk).update(status=RevisionStatusChoices.INVALID)
        ScriptFile.objects.create(project=self.project, source_path='hello.py')
        refreshed, created = service.refresh_revision_script_files(first)
        self.assertTrue(created)
        first.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.INVALID)
        self.assertEqual(refreshed.status, RevisionStatusChoices.MATERIALIZED)


class ActivationSnapshotTestCase(StorageServiceMixin, TestCase):
    """Activation trusts the entrypoint snapshot only after its return-trip check."""

    def test_activation_rejects_a_tampered_snapshot(self):
        revision = self.validated()
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(
            script_file_snapshot=[{'script_file': 1, 'source_path': '../outside.py'}]
        )
        with self.assertRaises(RevisionCorruptError):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision)

    def test_activation_rejects_a_snapshot_swapped_after_its_return_trip_check(self):
        # The pre-lock check proves the snapshot matched its digest when it was read. Only
        # the locked comparison can prove it still does, so the swap goes in that window
        # and leaves entrypoint_digest untouched.
        script_file = ScriptFile.objects.create(project=self.project, source_path='hello.py')
        revision = self.validated()
        real_verify = store.verify_revision_tree

        def verify_then_swap_snapshot(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            ScriptProjectRevision.objects.filter(pk=revision.pk).update(
                script_file_snapshot=[{'script_file': script_file.pk, 'source_path': 'swapped.py'}]
            )
            return result

        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=verify_then_swap_snapshot),
            self.assertRaises(ActivationError) as ctx,
        ):
            service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertIn('changed while', str(ctx.exception))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_activation_accepts_a_revision_with_a_sound_snapshot(self):
        script_file = ScriptFile.objects.create(project=self.project, source_path='hello.py')
        revision = self.validated()
        activated = service.promote_revision(revision, on_promote=promote_without_synchronizing)
        self.assertEqual(activated.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(activated.script_file_snapshot, [{'script_file': script_file.pk, 'source_path': 'hello.py'}])
