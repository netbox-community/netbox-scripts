import os
import pathlib
import tempfile
import uuid
from unittest import mock

from django.test import TestCase

from netbox_custom_scripts import constants
from netbox_custom_scripts.storage import store
from netbox_custom_scripts.storage.config import StorageLimits
from netbox_custom_scripts.storage.exceptions import LimitExceededError, StorageError, UnsafePathError


def limits(**overrides):
    values = {
        'max_file_size': constants.DEFAULT_MAX_FILE_SIZE,
        'max_project_size': constants.DEFAULT_MAX_PROJECT_SIZE,
        'max_file_count': constants.DEFAULT_MAX_FILE_COUNT,
    }
    values.update(overrides)
    return StorageLimits(**values)


class DirectoryWalkTestCase(TestCase):
    def test_iter_directory_files_yields_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'a.py').write_bytes(b'aaa')
            (root / 'sub').mkdir()
            (root / 'sub' / 'b.py').write_bytes(b'bbb')
            result = dict(store.iter_directory_files(root, limits()))
        self.assertEqual(result, {'a.py': b'aaa', 'sub/b.py': b'bbb'})

    def test_iter_directory_files_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'real.py').write_bytes(b'x')
            (root / 'link.py').symlink_to(root / 'real.py')
            with self.assertRaises(UnsafePathError) as ctx:
                list(store.iter_directory_files(root, limits()))
            self.assertEqual(ctx.exception.code, 'symlink')

    def test_iter_directory_files_rejects_special_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            os.mkfifo(root / 'pipe')
            with self.assertRaises(UnsafePathError) as ctx:
                list(store.iter_directory_files(root, limits()))
            self.assertEqual(ctx.exception.code, 'special_file')

    def test_iter_directory_files_rejects_missing_root(self):
        with self.assertRaises(StorageError):
            list(store.iter_directory_files('/ncs/storage/does/not/exist', limits()))

    def test_iter_directory_files_rejects_file_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_root = pathlib.Path(tmp) / 'a.py'
            file_root.write_bytes(b'x')
            with self.assertRaises(StorageError):
                list(store.iter_directory_files(file_root, limits()))

    def test_iter_directory_files_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / 'real_dir'
            real.mkdir()
            link = pathlib.Path(tmp) / 'link_dir'
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(UnsafePathError) as ctx:
                list(store.iter_directory_files(link, limits()))
            self.assertEqual(ctx.exception.code, 'symlink')

    def test_iter_directory_files_converts_walk_errors_to_storage_error(self):
        def failing_walk(top, followlinks=False, onerror=None):
            onerror(PermissionError('denied'))
            return iter(())

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.store.os.walk', failing_walk),
            self.assertRaises(StorageError),
        ):
            list(store.iter_directory_files(tmp, limits()))

    def test_iter_directory_files_converts_file_read_errors_to_storage_error(self):
        def walk_with_missing_file(top, followlinks=False, onerror=None):
            yield str(top), [], ['ghost.py']

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.store.os.walk', walk_with_missing_file),
            self.assertRaises(StorageError),
        ):
            list(store.iter_directory_files(tmp, limits()))

    def test_iter_directory_files_rejects_file_over_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'big.py').write_bytes(b'12345')
            with self.assertRaises(LimitExceededError) as ctx:
                list(store.iter_directory_files(root, limits(max_file_size=4)))
            self.assertEqual(ctx.exception.code, 'file_too_large')

    def test_iter_directory_files_rejects_too_many_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ('a.py', 'b.py', 'c.py'):
                (root / name).write_bytes(b'x')
            with self.assertRaises(LimitExceededError) as ctx:
                list(store.iter_directory_files(root, limits(max_file_count=2)))
            self.assertEqual(ctx.exception.code, 'too_many_files')

    def test_iter_directory_files_rejects_total_over_project_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'a.py').write_bytes(b'123')
            (root / 'b.py').write_bytes(b'456')
            with self.assertRaises(LimitExceededError) as ctx:
                list(store.iter_directory_files(root, limits(max_project_size=4)))
            self.assertEqual(ctx.exception.code, 'project_too_large')


STORAGE_KEY = uuid.UUID('11111111-2222-3333-4444-555555555555')
DIGEST = 'a' * 64
OTHER_DIGEST = 'b' * 64


class RevisionStoreTestCase(TestCase):
    def test_write_staged_revision_creates_revision_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'aaa'})
            project = pathlib.Path(tmp) / str(STORAGE_KEY)
            self.assertEqual(destination, project / 'revisions' / DIGEST)
            self.assertEqual((destination / 'a.py').read_bytes(), b'aaa')
            self.assertEqual(list((project / 'staging').iterdir()), [])

    def test_write_staged_revision_writes_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'pkg/__init__.py': b'', 'pkg/deep/mod.py': b'body'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, files)
            self.assertEqual((destination / 'pkg' / '__init__.py').read_bytes(), b'')
            self.assertEqual((destination / 'pkg' / 'deep' / 'mod.py').read_bytes(), b'body')

    def test_write_staged_revision_canonicalizes_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'./pkg//mod.py': b'body'})
            self.assertEqual((destination / 'pkg' / 'mod.py').read_bytes(), b'body')

    def test_write_staged_revision_rejects_duplicate_canonical_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'mod.py': b'first', './mod.py': b'second'}
            with self.assertRaises(UnsafePathError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, files)
            self.assertEqual(ctx.exception.code, 'duplicate_path')
            self.assertFalse((pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions' / DIGEST).exists())

    def test_write_staged_revision_is_idempotent_when_destination_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            destination = store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'first'})
            again = store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'second'})
            self.assertEqual(again, destination)
            self.assertEqual((destination / 'a.py').read_bytes(), b'first')

    def test_write_staged_revision_leaves_no_digest_directory_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(store.Path, 'write_bytes', side_effect=OSError('no space left on device')),
                self.assertRaises(OSError),
            ):
                store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'aaa'})
            project = pathlib.Path(tmp) / str(STORAGE_KEY)
            self.assertFalse((project / 'revisions' / DIGEST).exists())
            self.assertEqual(list((project / 'staging').iterdir()), [])

    def test_write_staged_revision_rejects_a_traversal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(UnsafePathError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'../escape.py': b'x'})
            self.assertEqual(ctx.exception.code, 'path_traversal')
            self.assertFalse((pathlib.Path(tmp) / 'escape.py').exists())

    def test_write_staged_revision_rejects_a_path_escaping_the_revision_root(self):
        # normalize_source_path already rejects traversal, so the resolved-path gate is only
        # reachable with that front check bypassed.
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.store.normalize_source_path', return_value='../escape.py'),
            self.assertRaises(UnsafePathError) as ctx,
        ):
            store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'x'})
        self.assertEqual(ctx.exception.code, 'escapes_root')

    def test_delete_revision_directory_removes_only_that_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'a'})
            store.write_staged_revision(tmp, STORAGE_KEY, OTHER_DIGEST, {'b.py': b'b'})
            store.delete_revision_directory(tmp, STORAGE_KEY, DIGEST)
            revisions = pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions'
            self.assertEqual([entry.name for entry in revisions.iterdir()], [OTHER_DIGEST])

    def test_delete_revision_directory_tolerates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.delete_revision_directory(tmp, STORAGE_KEY, DIGEST)

    def test_delete_project_directory_removes_all_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.write_staged_revision(tmp, STORAGE_KEY, DIGEST, {'a.py': b'a'})
            store.write_staged_revision(tmp, STORAGE_KEY, OTHER_DIGEST, {'b.py': b'b'})
            store.delete_project_directory(tmp, STORAGE_KEY)
            self.assertFalse((pathlib.Path(tmp) / str(STORAGE_KEY)).exists())

    def test_delete_project_directory_tolerates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.delete_project_directory(tmp, STORAGE_KEY)
