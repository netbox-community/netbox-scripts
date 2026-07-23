import os
import pathlib
import tempfile
import unicodedata
import uuid
from unittest import mock

from django.test import TestCase, override_settings

from netbox_custom_scripts.storage import paths
from netbox_custom_scripts.storage.exceptions import LimitExceededError, StorageError, UnsafePathError


def plugin_config(**settings):
    return {'netbox_custom_scripts': settings}


class PathSafetyTestCase(TestCase):
    def test_normalize_source_path_canonical_forms(self):
        cases = {
            'hello.py': 'hello.py',
            'scripts/hello.py': 'scripts/hello.py',
            './scripts/hello.py': 'scripts/hello.py',
            'scripts//hello.py': 'scripts/hello.py',
            'scripts/./hello.py': 'scripts/hello.py',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(paths.normalize_source_path(raw), expected)

    def test_rejects_leading_or_trailing_whitespace(self):
        for raw in (' foo.py', 'foo.py ', 'foo.py\n', '\tfoo.py'):
            with self.subTest(raw=raw):
                with self.assertRaises(UnsafePathError) as ctx:
                    paths.normalize_source_path(raw)
                self.assertEqual(ctx.exception.code, 'path_traversal')

    def test_rejects_trailing_slash(self):
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path('scripts/')
        self.assertEqual(ctx.exception.code, 'path_traversal')

    def test_rejects_absolute_path(self):
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path('/etc/passwd')
        self.assertEqual(ctx.exception.code, 'absolute_path')
        self.assertEqual(ctx.exception.path, '/etc/passwd')

    def test_rejects_windows_drive_qualified_paths(self):
        for raw in ('C:/Windows/system32/config.py', 'C:relative.py'):
            with self.subTest(raw=raw):
                with self.assertRaises(UnsafePathError) as ctx:
                    paths.normalize_source_path(raw)
                self.assertEqual(ctx.exception.code, 'absolute_path')

    def test_rejects_traversal_segment(self):
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path('scripts/../../etc/passwd')
        self.assertEqual(ctx.exception.code, 'path_traversal')

    def test_rejects_backslash_and_control_characters(self):
        for raw in ('scripts\\hello.py', 'scripts/a\x00b.py', 'scripts/a\tb.py'):
            with self.subTest(raw=raw):
                with self.assertRaises(UnsafePathError) as ctx:
                    paths.normalize_source_path(raw)
                self.assertEqual(ctx.exception.code, 'path_traversal')

    def test_normalizes_unicode_to_nfc(self):
        decomposed = unicodedata.normalize('NFD', 'café.py')
        composed = unicodedata.normalize('NFC', 'café.py')
        self.assertNotEqual(decomposed, composed)
        self.assertEqual(paths.normalize_source_path(decomposed), composed)
        self.assertEqual(paths.normalize_source_path(decomposed), paths.normalize_source_path(composed))

    def test_path_helpers_reject_unsafe_identifiers(self):
        with self.assertRaises(UnsafePathError):
            paths.project_directory('../etc')
        with self.assertRaises(UnsafePathError):
            paths.revision_directory('..', '../../escape')
        valid_key = uuid.uuid4()
        for bad_digest in ('../../escape', 'g' * 64, 'abc/def', 'A' * 64):
            with self.subTest(bad_digest=bad_digest), self.assertRaises(UnsafePathError):
                paths.revision_directory(valid_key, bad_digest)
        for bad_token in ('../escape', 'xyz', 'a/b'):
            with self.subTest(bad_token=bad_token), self.assertRaises(UnsafePathError):
                paths.staging_directory(valid_key, bad_token)

    def test_project_directory_and_revision_directory_are_pure_functions_of_storage_key_and_digest(self):
        storage_key = uuid.UUID('12345678-1234-5678-1234-567812345678')
        digest = 'a' * 64
        token = 'b' * 32
        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(PLUGINS_CONFIG=plugin_config(project_root=tmp)),
        ):
            root = pathlib.Path(tmp)
            self.assertEqual(paths.project_directory(storage_key), root / str(storage_key))
            self.assertEqual(
                paths.revision_directory(storage_key, digest),
                root / str(storage_key) / 'revisions' / digest,
            )
            self.assertEqual(
                paths.staging_directory(storage_key, token),
                root / str(storage_key) / 'staging' / token,
            )
            self.assertEqual(
                paths.revision_directory(storage_key, digest),
                paths.revision_directory(storage_key, digest),
            )


class DirectoryWalkTestCase(TestCase):
    def test_iter_directory_files_yields_regular_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'a.py').write_bytes(b'aaa')
            (root / 'sub').mkdir()
            (root / 'sub' / 'b.py').write_bytes(b'bbb')
            result = dict(paths.iter_directory_files(root))
        self.assertEqual(result, {'a.py': b'aaa', 'sub/b.py': b'bbb'})

    def test_iter_directory_files_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'real.py').write_bytes(b'x')
            (root / 'link.py').symlink_to(root / 'real.py')
            with self.assertRaises(UnsafePathError) as ctx:
                list(paths.iter_directory_files(root))
            self.assertEqual(ctx.exception.code, 'symlink')

    def test_iter_directory_files_rejects_special_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            os.mkfifo(root / 'pipe')
            with self.assertRaises(UnsafePathError) as ctx:
                list(paths.iter_directory_files(root))
            self.assertEqual(ctx.exception.code, 'special_file')

    def test_iter_directory_files_rejects_missing_root(self):
        with self.assertRaises(StorageError):
            list(paths.iter_directory_files('/ncs/storage/does/not/exist'))

    def test_iter_directory_files_rejects_file_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_root = pathlib.Path(tmp) / 'a.py'
            file_root.write_bytes(b'x')
            with self.assertRaises(StorageError):
                list(paths.iter_directory_files(file_root))

    def test_iter_directory_files_rejects_symlink_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / 'real_dir'
            real.mkdir()
            link = pathlib.Path(tmp) / 'link_dir'
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(UnsafePathError) as ctx:
                list(paths.iter_directory_files(link))
            self.assertEqual(ctx.exception.code, 'symlink')

    def test_iter_directory_files_converts_walk_errors_to_storage_error(self):
        def failing_walk(top, followlinks=False, onerror=None):
            onerror(PermissionError('denied'))
            return iter(())

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.paths.os.walk', failing_walk),
            self.assertRaises(StorageError),
        ):
            list(paths.iter_directory_files(tmp))

    def test_iter_directory_files_converts_file_read_errors_to_storage_error(self):
        def walk_with_missing_file(top, followlinks=False, onerror=None):
            yield str(top), [], ['ghost.py']

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.paths.os.walk', walk_with_missing_file),
            self.assertRaises(StorageError),
        ):
            list(paths.iter_directory_files(tmp))

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=4))
    def test_iter_directory_files_rejects_file_over_size_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'big.py').write_bytes(b'12345')
            with self.assertRaises(LimitExceededError) as ctx:
                list(paths.iter_directory_files(root))
            self.assertEqual(ctx.exception.code, 'file_too_large')

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_count=2))
    def test_iter_directory_files_rejects_too_many_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            for name in ('a.py', 'b.py', 'c.py'):
                (root / name).write_bytes(b'x')
            with self.assertRaises(LimitExceededError) as ctx:
                list(paths.iter_directory_files(root))
            self.assertEqual(ctx.exception.code, 'too_many_files')

    @override_settings(PLUGINS_CONFIG=plugin_config(max_project_size=4))
    def test_iter_directory_files_rejects_total_over_project_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / 'a.py').write_bytes(b'123')
            (root / 'b.py').write_bytes(b'456')
            with self.assertRaises(LimitExceededError) as ctx:
                list(paths.iter_directory_files(root))
            self.assertEqual(ctx.exception.code, 'project_too_large')
