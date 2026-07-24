import pathlib
import unicodedata
import uuid

from django.test import TestCase

from netbox_custom_scripts.storage import paths
from netbox_custom_scripts.storage.exceptions import UnsafePathError

PROJECT_ROOT = pathlib.Path('/ncs/projects')


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
            paths.project_directory(PROJECT_ROOT, '../etc')
        with self.assertRaises(UnsafePathError):
            paths.revision_directory(PROJECT_ROOT, '..', '../../escape')
        valid_key = uuid.uuid4()
        for bad_digest in ('../../escape', 'g' * 64, 'abc/def', 'A' * 64):
            with self.subTest(bad_digest=bad_digest), self.assertRaises(UnsafePathError):
                paths.revision_directory(PROJECT_ROOT, valid_key, bad_digest)
        for bad_token in ('../escape', 'xyz', 'a/b'):
            with self.subTest(bad_token=bad_token), self.assertRaises(UnsafePathError):
                paths.staging_directory(PROJECT_ROOT, valid_key, bad_token)

    def test_path_helpers_are_pure_functions_of_their_arguments(self):
        storage_key = uuid.UUID('12345678-1234-5678-1234-567812345678')
        digest = 'a' * 64
        token = 'b' * 32
        self.assertEqual(paths.project_directory(PROJECT_ROOT, storage_key), PROJECT_ROOT / str(storage_key))
        self.assertEqual(
            paths.revision_directory(PROJECT_ROOT, storage_key, digest),
            PROJECT_ROOT / str(storage_key) / 'revisions' / digest,
        )
        self.assertEqual(
            paths.staging_directory(PROJECT_ROOT, storage_key, token),
            PROJECT_ROOT / str(storage_key) / 'staging' / token,
        )
        self.assertEqual(
            paths.revision_directory(PROJECT_ROOT, storage_key, digest),
            paths.revision_directory(PROJECT_ROOT, storage_key, digest),
        )


class SourcePathPolicyTestCase(TestCase):
    """
    Cover the limits that keep a source path materializable on every supported host.

    Each of these passed manifest construction before and then failed at write time as a
    filesystem error, which the staging service recorded as STORAGE_FAILED even though no
    retry of the same content could ever succeed.
    """

    def test_rejects_a_component_over_the_byte_limit(self):
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path('x' * (paths.MAX_PATH_COMPONENT_BYTES + 1) + '.py')
        self.assertEqual(ctx.exception.code, 'path_component_too_long')

    def test_accepts_a_component_at_the_byte_limit(self):
        name = 'x' * paths.MAX_PATH_COMPONENT_BYTES
        self.assertEqual(paths.normalize_source_path(name), name)

    def test_counts_component_length_in_utf8_bytes_not_characters(self):
        # NAME_MAX is a byte limit, so a name well under it in characters can still be over.
        name = 'é' * paths.MAX_PATH_COMPONENT_BYTES
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path(name)
        self.assertEqual(ctx.exception.code, 'path_component_too_long')

    def test_rejects_a_path_over_the_total_byte_limit(self):
        segments = ['d' * 60] * ((paths.MAX_PATH_BYTES // 61) + 2)
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path('/'.join(segments) + '/mod.py')
        self.assertEqual(ctx.exception.code, 'path_too_long')

    def test_rejects_a_path_over_the_depth_limit(self):
        deep = '/'.join(f'd{index}' for index in range(paths.MAX_PATH_DEPTH)) + '/mod.py'
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path(deep)
        self.assertEqual(ctx.exception.code, 'path_too_deep')

    def test_accepts_a_path_at_the_depth_limit(self):
        deep = '/'.join(f'd{index}' for index in range(paths.MAX_PATH_DEPTH - 1)) + '/mod.py'
        self.assertEqual(paths.normalize_source_path(deep), deep)
