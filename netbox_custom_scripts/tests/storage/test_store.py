import os
import pathlib
import tempfile
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
