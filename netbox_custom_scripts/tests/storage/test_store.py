import errno
import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import TestCase

from netbox_custom_scripts.storage import store
from netbox_custom_scripts.storage.exceptions import RevisionCorruptError, StorageError, UnsafePathError
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.storage.paths import revision_prefix

STORAGE_KEY = uuid.UUID('9f1c6d24-0b2a-4d3e-8f57-2c9a4b6e1d80')


def manifest_for(files):
    """Return the manifest entries describing a mapping of canonical path to content."""
    return sorted(
        (
            {'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
            for path, content in files.items()
        ),
        key=lambda entry: entry['path'],
    )


def stored_paths(storage, prefix):
    """
    Return every path actually stored under one prefix, relative to it.

    Tests enumerate the backend to pin exactly what an operation left behind. Production code
    works from the manifest and never lists, which is the property the backend contract tests
    hold it to, so this walk lives here.
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


class RefusingStorage(InMemoryStorage):
    """A backend that fails one named operation, standing in for an unreachable store."""

    def __init__(self, failing, error=None, **kwargs):
        super().__init__(**kwargs)
        self.failing = failing
        self.error = error or OSError('the backend is unreachable')

    def _fail(self, name):
        if name in self.failing:
            raise self.error

    def save(self, name, content, max_length=None):
        self._fail('save')
        return super().save(name, content, max_length=max_length)

    def exists(self, name):
        self._fail('exists')
        return super().exists(name)

    def open(self, name, mode='rb'):
        self._fail('open')
        return super().open(name, mode)

    def delete(self, name):
        self._fail('delete')
        return super().delete(name)

    def listdir(self, path):
        self._fail('listdir')
        return super().listdir(path)


class RenamingStorage(InMemoryStorage):
    """A backend that parks one save under an invented name, as a backend resolving a collision."""

    def __init__(self, divert, plant=None, keep_stray=False, **kwargs):
        super().__init__(**kwargs)
        self.divert = divert
        self.plant = plant
        self.keep_stray = keep_stray

    def save(self, name, content, max_length=None):
        if name == self.divert:
            # A competing writer occupies the canonical key in the window between the caller's
            # emptiness check and this save, so the backend names this write something else.
            if self.plant is not None:
                super().save(name, ContentFile(self.plant), max_length=max_length)
            return super().save(f'{name}.alias', content, max_length=max_length)
        return super().save(name, content, max_length=max_length)

    def delete(self, name):
        if self.keep_stray and name.endswith('.alias'):
            raise OSError('the backend refused the removal')
        return super().delete(name)


class BottomlessStorage(InMemoryStorage):
    """Serve one key as an endless stream whose metadata reports the recorded size."""

    def __init__(self, bottomless_key, reported_size, **kwargs):
        super().__init__(**kwargs)
        self.bottomless_key = bottomless_key
        self.reported_size = reported_size
        self.served = 0

    def size(self, name):
        if name != self.bottomless_key:
            return super().size(name)
        return self.reported_size

    def open(self, name, mode='rb'):
        if name != self.bottomless_key:
            return super().open(name, mode)
        backend = self

        class EndlessHandle:
            """A read handle that never runs out, as a replaced object of absurd size."""

            def read(self, n):
                backend.served += n
                return b'x' * n

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return EndlessHandle()


class OpenRecordingStorage(InMemoryStorage):
    """Record every key handed to open(), so a test can assert what was never read."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.opened = []

    def open(self, name, mode='rb'):
        self.opened.append(name)
        return super().open(name, mode)


class SizelessStorage(InMemoryStorage):
    """A backend that cannot report object sizes, which verification survives by bounded reads."""

    def size(self, name):
        raise NotImplementedError('This backend reports no sizes.')


class SizelessBottomlessStorage(BottomlessStorage):
    """An endless stream with no size support, the worst case the read bound must absorb."""

    def size(self, name):
        raise NotImplementedError('This backend reports no sizes.')


class KeyRefusingStorage(InMemoryStorage):
    """A backend that refuses to remove specific keys."""

    def __init__(self, refuse, **kwargs):
        super().__init__(**kwargs)
        self.refuse = refuse

    def delete(self, name):
        if name.endswith(tuple(self.refuse)):
            raise OSError('the backend refused the removal')
        return super().delete(name)


class RevisionStoreTestCase(TestCase):
    """Cover writing, verifying, and removing revision content through a storage backend."""

    files = {'hello.py': b'print("hi")', 'pkg/util.py': b'VALUE = 1'}

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        self.prefix = revision_prefix(STORAGE_KEY, self.digest)

    def write(self, storage=None, files=None, manifest=None, digest=None):
        return store.write_revision(
            storage or self.storage,
            STORAGE_KEY,
            digest or self.digest,
            self.files if files is None else files,
            manifest or self.manifest,
        )

    def stored_keys(self, storage=None):
        """Return every key held under the revision prefix, relative to it."""
        return stored_paths(storage or self.storage, self.prefix)

    def test_a_write_stores_every_manifest_entry_under_the_revision_prefix(self):
        returned = self.write()
        self.assertEqual(returned, self.prefix)
        self.assertEqual(self.stored_keys(), set(self.files))
        for path, content in self.files.items():
            with self.storage.open(f'{self.prefix}{path}', 'rb') as handle:
                self.assertEqual(handle.read(), content)

    def test_a_write_verifies_the_tree_before_returning(self):
        self.write()
        self.assertEqual(store.verify_revision_tree(self.storage, STORAGE_KEY, self.digest, self.manifest), self.prefix)

    def test_re_writing_an_identical_tree_never_produces_an_aliased_key(self):
        # Storage.save() renames on collision, which in a content-addressed store means a file
        # written where nothing will look for it. The key set must be exactly the manifest's.
        self.write()
        self.write()
        self.assertEqual(self.stored_keys(), set(self.files))

    def test_a_retry_completes_a_partially_written_tree(self):
        self.storage.save(f'{self.prefix}hello.py', ContentFile(self.files['hello.py']))
        self.write()
        self.assertEqual(self.stored_keys(), set(self.files))

    def test_a_retry_replaces_a_key_holding_the_wrong_content(self):
        self.storage.save(f'{self.prefix}hello.py', ContentFile(b'something else entirely'))
        self.write()
        self.assertEqual(self.stored_keys(), set(self.files))
        with self.storage.open(f'{self.prefix}hello.py', 'rb') as handle:
            self.assertEqual(handle.read(), self.files['hello.py'])

    def test_an_alternate_name_save_succeeds_when_a_competitor_stored_the_content(self):
        # Between the emptiness check and the save, another writer of the same digest can
        # install the canonical key. The revision this call was asked to produce then exists,
        # so the parked stray is removed and the write counts as done.
        storage = RenamingStorage(divert=f'{self.prefix}hello.py', plant=self.files['hello.py'])
        self.assertEqual(self.write(storage=storage), self.prefix)
        self.assertEqual(self.stored_keys(storage), set(self.files))
        with storage.open(f'{self.prefix}hello.py', 'rb') as handle:
            self.assertEqual(handle.read(), self.files['hello.py'])

    def test_an_alternate_name_save_fails_when_the_key_holds_something_else(self):
        storage = RenamingStorage(divert=f'{self.prefix}hello.py', plant=b'not the recorded content')
        with self.assertRaises(StorageError) as ctx:
            self.write(storage=storage)
        self.assertIn('instead of the key', str(ctx.exception))
        # The stray alias is gone, and the occupant of the canonical key is another writer's
        # property, so it is left in place for that writer's own verification to judge.
        self.assertEqual(self.stored_keys(storage), {'hello.py'})

    def test_a_stray_the_backend_keeps_is_logged_and_the_competitor_success_stands(self):
        # The stray sits in no manifest, so nothing can rediscover it later. The error log
        # line naming its exact key is its one record until the housekeeping reconciler owns
        # orphans, and it must not cost the write its otherwise correct outcome.
        storage = RenamingStorage(divert=f'{self.prefix}hello.py', plant=self.files['hello.py'], keep_stray=True)
        with self.assertLogs(store.logger, 'ERROR') as logged:
            self.assertEqual(self.write(storage=storage), self.prefix)
        self.assertIn(f'{self.prefix}hello.py.alias', logged.output[0])
        self.assertEqual(self.stored_keys(storage), {*self.files, 'hello.py.alias'})

    def test_a_backend_failure_on_exists_surfaces_as_a_storage_error(self):
        # exists() is the first backend call a write makes, and a cloud backend can raise
        # something there that is neither OSError nor StorageError. The wrapper is what lets
        # the staging service record the failure instead of stranding the row in STAGING.
        storage = RefusingStorage(failing={'exists'}, error=RuntimeError('the sdk gave up'))
        with self.assertRaises(StorageError) as ctx:
            self.write(storage=storage)
        self.assertIn(self.prefix, str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, RuntimeError)

    def test_a_write_canonicalizes_the_paths_it_is_given(self):
        self.write(files={'./hello.py': self.files['hello.py'], 'pkg//util.py': self.files['pkg/util.py']})
        self.assertEqual(self.stored_keys(), set(self.files))

    def test_a_write_refuses_two_source_paths_that_resolve_to_one_key(self):
        with self.assertRaises(UnsafePathError) as ctx:
            self.write(files={'pkg/util.py': b'VALUE = 1', './pkg/util.py': b'VALUE = 1'})
        self.assertEqual(ctx.exception.code, 'duplicate_path')

    def test_a_write_refuses_a_manifest_the_digest_does_not_address(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.write(digest='a' * 64)
        self.assertIn('digest_mismatch', ctx.exception.reasons)
        self.assertEqual(self.stored_keys(), set())

    def test_a_write_refuses_a_manifest_naming_content_it_was_not_given(self):
        manifest = manifest_for({**self.files, 'missing.py': b'absent'})
        with self.assertRaises(StorageError):
            self.write(manifest=manifest, digest=compute_digest(manifest))

    def test_a_tree_that_fails_verification_is_left_for_the_next_attempt(self):
        # The manifest records content this call is not given, so verification fails after
        # the write. The keys stay: a concurrent writer of this digest may already count on
        # them, the row never reports MATERIALIZED, and a retry replaces what disagrees.
        manifest = manifest_for({'hello.py': b'print("hi")', 'pkg/util.py': b'VALUE = 2'})
        digest = compute_digest(manifest)
        prefix = revision_prefix(STORAGE_KEY, digest)
        with self.assertRaises(RevisionCorruptError):
            store.write_revision(self.storage, STORAGE_KEY, digest, self.files, manifest)
        self.assertEqual(stored_paths(self.storage, prefix), set(self.files))

    def test_a_backend_failure_on_write_surfaces_as_a_storage_error_naming_the_key(self):
        storage = RefusingStorage(failing={'save'})
        with self.assertRaises(StorageError) as ctx:
            self.write(storage=storage)
        self.assertIn(self.prefix, str(ctx.exception))

    def test_a_backend_failure_on_read_surfaces_as_a_storage_error_naming_the_key(self):
        self.write()
        storage = RefusingStorage(failing={'open'})
        storage.save(f'{self.prefix}hello.py', ContentFile(self.files['hello.py']))
        with self.assertRaises(StorageError) as ctx:
            store.verify_revision_tree(storage, STORAGE_KEY, self.digest, self.manifest)
        self.assertIn('hello.py', str(ctx.exception))


class RevisionVerificationTestCase(TestCase):
    """Cover what verification reports when stored content and its manifest disagree."""

    files = {'hello.py': b'print("hi")', 'pkg/util.py': b'VALUE = 1'}

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        self.prefix = revision_prefix(STORAGE_KEY, self.digest)
        store.write_revision(self.storage, STORAGE_KEY, self.digest, self.files, self.manifest)

    def verify(self):
        return store.verify_revision_tree(self.storage, STORAGE_KEY, self.digest, self.manifest)

    def assert_reasons(self, *expected):
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.verify()
        self.assertEqual(sorted(ctx.exception.reasons), sorted(expected))

    def test_an_intact_tree_verifies(self):
        self.assertEqual(self.verify(), self.prefix)

    def test_a_revision_that_is_entirely_absent_reports_one_reason(self):
        # An operator whose revision has vanished should not read one line per file to learn it.
        for path in self.files:
            self.storage.delete(f'{self.prefix}{path}')
        self.assert_reasons('prefix_missing')

    def test_a_missing_file_is_reported(self):
        self.storage.delete(f'{self.prefix}hello.py')
        self.assert_reasons('missing:hello.py')

    def test_a_size_mismatch_is_reported(self):
        self.storage.delete(f'{self.prefix}hello.py')
        self.storage.save(f'{self.prefix}hello.py', ContentFile(b'print("hi") and more'))
        self.assert_reasons('size_mismatch:hello.py')

    def test_a_checksum_mismatch_is_reported(self):
        replacement = b'X' * len(self.files['hello.py'])
        self.storage.delete(f'{self.prefix}hello.py')
        self.storage.save(f'{self.prefix}hello.py', ContentFile(replacement))
        self.assert_reasons('checksum_mismatch:hello.py')

    def test_a_key_the_manifest_does_not_name_is_inert(self):
        # Verification reads manifest keys only and materialization copies manifest entries
        # only, so a stray object under the prefix can never become an importable module.
        # Reclaiming it belongs to a future housekeeping reconciler.
        self.storage.save(f'{self.prefix}pkg/sneaky.py', ContentFile(b'import os'))
        self.assertEqual(self.verify(), self.prefix)

    def test_every_mismatch_is_reported_at_once(self):
        self.storage.delete(f'{self.prefix}hello.py')
        self.storage.delete(f'{self.prefix}pkg/util.py')
        self.storage.save(f'{self.prefix}pkg/util.py', ContentFile(b'X' * len(self.files['pkg/util.py'])))
        self.assert_reasons('missing:hello.py', 'checksum_mismatch:pkg/util.py')

    def test_verification_reads_no_further_than_the_recorded_size(self):
        # The backend reports the recorded size here, so the metadata preflight passes and
        # the stream itself is longer than it claims. The read bound is the layer that
        # settles that case, one byte past the recorded size.
        expected = next(entry for entry in self.manifest if entry['path'] == 'hello.py')
        storage = BottomlessStorage(bottomless_key=f'{self.prefix}hello.py', reported_size=expected['size'])
        storage.save(f'{self.prefix}pkg/util.py', ContentFile(self.files['pkg/util.py']))
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.verify_revision_tree(storage, STORAGE_KEY, self.digest, self.manifest)
        self.assertEqual(sorted(ctx.exception.reasons), ['size_mismatch:hello.py'])
        self.assertLessEqual(storage.served, expected['size'] + 1)

    def test_an_oversized_object_is_rejected_from_metadata_without_being_opened(self):
        # On a remote backend, opening an object can download it whole before the first read
        # is served, so a wrong reported size has to settle the verdict before any open.
        storage = OpenRecordingStorage()
        store.write_revision(storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        storage.delete(f'{self.prefix}hello.py')
        storage.save(f'{self.prefix}hello.py', ContentFile(b'print("hi") plus a replacement tail'))
        storage.opened.clear()
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.verify_revision_tree(storage, STORAGE_KEY, self.digest, self.manifest)
        self.assertEqual(sorted(ctx.exception.reasons), ['size_mismatch:hello.py'])
        self.assertNotIn(f'{self.prefix}hello.py', storage.opened)
        # The intact entry still had its checksum read, metadata cannot vouch for bytes.
        self.assertIn(f'{self.prefix}pkg/util.py', storage.opened)

    def test_a_backend_without_size_support_still_rejects_an_oversized_object(self):
        # The preflight is best effort, the bounded read still rejects where size() is absent.
        storage = SizelessStorage()
        store.write_revision(storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        storage.delete(f'{self.prefix}hello.py')
        storage.save(f'{self.prefix}hello.py', ContentFile(b'print("hi") plus a replacement tail'))
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.verify_revision_tree(storage, STORAGE_KEY, self.digest, self.manifest)
        self.assertEqual(sorted(ctx.exception.reasons), ['size_mismatch:hello.py'])

    def test_a_manifest_that_does_not_address_its_digest_is_refused(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.verify_revision_tree(self.storage, STORAGE_KEY, 'b' * 64, self.manifest)
        self.assertIn('digest_mismatch', ctx.exception.reasons)

    def test_a_manifest_path_leaving_the_revision_is_refused_before_any_key_is_read(self):
        tampered = [{'path': '../escape.py', 'size': 1, 'sha256': 'a' * 64}]
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.verify_revision_tree(self.storage, STORAGE_KEY, None, tampered)
        self.assertEqual(ctx.exception.reasons, ('path_traversal:../escape.py',))


class ReadVerifiedTestCase(TestCase):
    """The in-memory verified read and the whole-tree read built on it."""

    files = {'hello.py': b'print("hi")', 'pkg/util.py': b'VALUE = 1'}

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        self.prefix = revision_prefix(STORAGE_KEY, self.digest)
        store.write_revision(self.storage, STORAGE_KEY, self.digest, self.files, self.manifest)

    def entry(self, path):
        return next(entry for entry in self.manifest if entry['path'] == path)

    def test_matching_content_is_returned(self):
        for path, expected in self.files.items():
            self.assertEqual(store.read_verified(self.storage, f'{self.prefix}{path}', self.entry(path)), expected)

    def test_a_missing_key_is_reported_as_corrupt(self):
        entry = self.entry('hello.py')
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.read_verified(self.storage, f'{self.prefix}absent.py', entry)
        self.assertIn('missing:hello.py', ctx.exception.reasons)

    def test_a_size_mismatch_is_reported(self):
        entry = dict(self.entry('hello.py'), size=self.entry('hello.py')['size'] + 1)
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.read_verified(self.storage, f'{self.prefix}hello.py', entry)
        self.assertIn('size_mismatch:hello.py', ctx.exception.reasons)

    def test_a_checksum_mismatch_is_reported(self):
        entry = dict(self.entry('hello.py'), sha256='b' * 64)
        with self.assertRaises(RevisionCorruptError) as ctx:
            store.read_verified(self.storage, f'{self.prefix}hello.py', entry)
        self.assertIn('checksum_mismatch:hello.py', ctx.exception.reasons)

    def test_the_whole_tree_is_returned_by_path(self):
        self.assertEqual(store.read_revision_tree(self.storage, STORAGE_KEY, self.digest, self.manifest), self.files)

    def test_a_tree_read_validates_the_manifest_first(self):
        # The paths come from a database row, so a manifest that does not match its own digest
        # must not get to decide which keys are read.
        with self.assertRaises(RevisionCorruptError):
            store.read_revision_tree(self.storage, STORAGE_KEY, 'c' * 64, self.manifest)

    def test_a_damaged_tree_raises_rather_than_returning_partial_content(self):
        store.delete_revision(self.storage, STORAGE_KEY, self.digest, ['pkg/util.py'])
        with self.assertRaises(RevisionCorruptError):
            store.read_revision_tree(self.storage, STORAGE_KEY, self.digest, self.manifest)


class CopyVerifiedTestCase(TestCase):
    """Cover the exported bounded verified read that feeds the runtime cache."""

    files = {'hello.py': b'print("hi")', 'pkg/util.py': b'VALUE = 1'}

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        self.prefix = revision_prefix(STORAGE_KEY, self.digest)
        store.write_revision(self.storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        self.workdir = Path(tempfile.mkdtemp(prefix='nbcs-copy-test-'))
        self.addCleanup(shutil.rmtree, self.workdir, True)

    def entry(self, path):
        return next(entry for entry in self.manifest if entry['path'] == path)

    def copy(self, storage=None, path='hello.py', destination=None):
        return store.copy_verified(storage or self.storage, f'{self.prefix}{path}', self.entry(path), destination)

    def test_the_destination_receives_exactly_the_verified_bytes(self):
        destination = self.workdir / 'hello.py'
        self.copy(destination=destination)
        self.assertEqual(destination.read_bytes(), self.files['hello.py'])

    def test_without_a_destination_the_read_is_verification_only(self):
        self.copy()
        self.assertEqual(list(self.workdir.iterdir()), [])

    def test_a_missing_object_is_reported_with_its_manifest_path(self):
        self.storage.delete(f'{self.prefix}hello.py')
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.copy(destination=self.workdir / 'out')
        self.assertEqual(ctx.exception.reasons, ('missing:hello.py',))

    def test_a_checksum_mismatch_is_rejected(self):
        self.storage.delete(f'{self.prefix}hello.py')
        self.storage.save(f'{self.prefix}hello.py', ContentFile(b'X' * len(self.files['hello.py'])))
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.copy(destination=self.workdir / 'out')
        self.assertEqual(ctx.exception.reasons, ('checksum_mismatch:hello.py',))

    def test_an_oversized_object_is_rejected_from_metadata_without_being_opened(self):
        storage = OpenRecordingStorage()
        store.write_revision(storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        storage.delete(f'{self.prefix}hello.py')
        storage.save(f'{self.prefix}hello.py', ContentFile(b'print("hi") plus a tail'))
        storage.opened.clear()
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.copy(storage=storage, destination=self.workdir / 'out')
        self.assertEqual(ctx.exception.reasons, ('size_mismatch:hello.py',))
        self.assertEqual(storage.opened, [])

    def test_a_backend_without_sizes_still_bounds_the_copy(self):
        # No metadata preflight is available here and the stream never ends, so the read
        # bound is the only thing standing between the copy and an unbounded download.
        expected = self.entry('hello.py')
        storage = SizelessBottomlessStorage(bottomless_key=f'{self.prefix}hello.py', reported_size=0)
        destination = self.workdir / 'out'
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.copy(storage=storage, destination=destination)
        self.assertEqual(ctx.exception.reasons, ('size_mismatch:hello.py',))
        self.assertLessEqual(storage.served, expected['size'] + 1)
        self.assertLessEqual(destination.stat().st_size, expected['size'] + 1)

    def test_a_backend_read_failure_surfaces_as_a_storage_error(self):
        storage = RefusingStorage(failing=set())
        store.write_revision(storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        storage.failing = {'open'}
        with self.assertRaises(StorageError):
            self.copy(storage=storage, destination=self.workdir / 'out')

    def test_a_destination_failure_raises_the_original_error_unwrapped(self):
        with self.assertRaises(FileNotFoundError):
            self.copy(destination=self.workdir / 'absent' / 'out')

    def test_a_mid_copy_destination_failure_never_masquerades_as_a_backend_error(self):
        # /dev/full accepts the open and refuses the bytes, exactly a disk filling mid-copy.
        # The chunk outsizes any write buffer, so the refusal lands on the streaming write.
        device = Path('/dev/full')
        if not device.exists():
            self.skipTest('this host offers no /dev/full device')
        files = {'big.bin': b'x' * (1024 * 1024)}
        manifest = manifest_for(files)
        digest = compute_digest(manifest)
        store.write_revision(self.storage, STORAGE_KEY, digest, files, manifest)
        with self.assertRaises(OSError) as ctx:
            store.copy_verified(self.storage, f'{revision_prefix(STORAGE_KEY, digest)}big.bin', manifest[0], device)
        self.assertEqual(ctx.exception.errno, errno.ENOSPC)
        self.assertNotIsInstance(ctx.exception, StorageError)


class RevisionRemovalTestCase(TestCase):
    """Cover reclaiming stored content by the exact keys a revision's manifest names."""

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest_paths = {}
        self.first = self.stage({'a.py': b'first'})
        self.second = self.stage({'b.py': b'second', 'pkg/c.py': b'nested'})

    def stage(self, files, storage=None):
        """Write one revision, record its manifest paths, and return its digest."""
        manifest = manifest_for(files)
        digest = compute_digest(manifest)
        store.write_revision(storage or self.storage, STORAGE_KEY, digest, files, manifest)
        self.manifest_paths[digest] = [entry['path'] for entry in manifest]
        return digest

    def delete(self, digest, storage=None):
        store.delete_revision(storage or self.storage, STORAGE_KEY, digest, self.manifest_paths[digest])

    def paths(self, digest):
        return stored_paths(self.storage, revision_prefix(STORAGE_KEY, digest))

    def test_deleting_one_revision_leaves_the_others_intact(self):
        self.delete(self.first)
        self.assertEqual(self.paths(self.first), set())
        self.assertEqual(self.paths(self.second), {'b.py', 'pkg/c.py'})

    def test_deleting_a_revision_removes_its_nested_keys(self):
        self.delete(self.second)
        self.assertEqual(self.paths(self.second), set())

    def test_deleting_every_revision_of_one_project_leaves_another_project_alone(self):
        other = uuid.UUID('1b7a1f60-52c8-4a0b-9f1e-6d3c8a2b5e47')
        manifest = manifest_for({'a.py': b'first'})
        digest = compute_digest(manifest)
        store.write_revision(self.storage, other, digest, {'a.py': b'first'}, manifest)
        self.delete(self.first)
        self.delete(self.second)
        self.assertEqual(stored_paths(self.storage, revision_prefix(other, digest)), {'a.py'})

    def test_deleting_content_that_is_already_gone_is_not_an_error(self):
        self.delete(self.first)
        self.delete(self.first)

    def test_a_path_already_gone_is_skipped_and_the_rest_are_removed(self):
        self.storage.delete(f'{revision_prefix(STORAGE_KEY, self.second)}b.py')
        self.delete(self.second)
        self.assertEqual(self.paths(self.second), set())

    def test_a_key_that_cannot_be_removed_is_reported_and_the_rest_are_attempted(self):
        storage = KeyRefusingStorage(refuse=('pkg/c.py',))
        digest = self.stage({'b.py': b'second', 'pkg/c.py': b'nested'}, storage=storage)
        with self.assertRaises(StorageError) as ctx:
            self.delete(digest, storage=storage)
        self.assertIn('pkg/c.py', str(ctx.exception))
        self.assertEqual(stored_paths(storage, revision_prefix(STORAGE_KEY, digest)), {'pkg/c.py'})

    def test_an_unusable_path_is_reported_and_the_rest_are_still_removed(self):
        # A payload edited by hand or left by an older release can carry a path no key can
        # be built from. It is reported without stopping the removal of every other key.
        with self.assertRaises(StorageError) as ctx:
            store.delete_revision(
                self.storage, STORAGE_KEY, self.second, ['../escape.py', *self.manifest_paths[self.second]]
            )
        self.assertIn('../escape.py', str(ctx.exception))
        self.assertEqual(self.paths(self.second), set())

    def test_the_lifecycle_never_needs_the_backend_to_list(self):
        # The manifest names every key, so a backend without directory semantics is enough
        # for writing, verifying, and removing a revision.
        storage = RefusingStorage(failing={'listdir'}, error=NotImplementedError('no listing here'))
        files = {'b.py': b'second', 'pkg/c.py': b'nested'}
        digest = self.stage(files, storage=storage)
        store.verify_revision_tree(storage, STORAGE_KEY, digest, manifest_for(files))
        self.delete(digest, storage=storage)
        for path in files:
            self.assertFalse(storage.exists(f'{revision_prefix(STORAGE_KEY, digest)}{path}'))

    def test_present_keys_reports_only_the_paths_still_in_the_store(self):
        self.storage.delete(f'{revision_prefix(STORAGE_KEY, self.second)}b.py')
        self.assertEqual(
            store.present_keys(self.storage, STORAGE_KEY, self.second, self.manifest_paths[self.second]),
            ['pkg/c.py'],
        )

    def test_present_keys_reports_nothing_for_content_already_reclaimed(self):
        paths = self.manifest_paths[self.second]
        self.delete(self.second)
        self.assertEqual(store.present_keys(self.storage, STORAGE_KEY, self.second, paths), [])

    def test_present_keys_never_needs_the_backend_to_list(self):
        storage = RefusingStorage(failing={'listdir'}, error=NotImplementedError('no listing here'))
        digest = self.stage({'b.py': b'second', 'pkg/c.py': b'nested'}, storage=storage)
        self.assertEqual(
            store.present_keys(storage, STORAGE_KEY, digest, self.manifest_paths[digest]),
            ['b.py', 'pkg/c.py'],
        )

    def test_present_keys_reports_a_backend_that_cannot_answer(self):
        storage = RefusingStorage(failing={'exists'}, error=RuntimeError('the sdk gave up'))
        with self.assertRaises(StorageError):
            store.present_keys(storage, STORAGE_KEY, self.first, self.manifest_paths[self.first])
