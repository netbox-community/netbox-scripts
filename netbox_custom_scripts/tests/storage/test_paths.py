import unicodedata
import uuid

from django.test import TestCase

from netbox_custom_scripts.storage import paths
from netbox_custom_scripts.storage.exceptions import UnsafePathError


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


class StorageKeyTestCase(TestCase):
    """Cover the keys that name a project's content in a storage backend."""

    storage_key = uuid.UUID('12345678-1234-5678-1234-567812345678')
    digest = 'a' * 64

    def test_the_keys_nest_project_then_revision_then_file(self):
        self.assertEqual(
            paths.project_prefix(self.storage_key),
            f'{paths.STORAGE_PREFIX}/12345678-1234-5678-1234-567812345678/',
        )
        self.assertEqual(
            paths.revision_prefix(self.storage_key, self.digest),
            f'{paths.STORAGE_PREFIX}/12345678-1234-5678-1234-567812345678/revisions/{self.digest}/',
        )
        self.assertEqual(
            paths.revision_key(self.storage_key, self.digest, 'scripts/hello.py'),
            f'{paths.STORAGE_PREFIX}/12345678-1234-5678-1234-567812345678/revisions/{self.digest}/scripts/hello.py',
        )

    def test_every_key_sits_under_the_plugin_prefix(self):
        # The configured backend may deliberately share a bucket with NetBox's media, so
        # nothing may land beside the prefix.
        for key in (
            paths.project_prefix(self.storage_key),
            paths.revision_prefix(self.storage_key, self.digest),
            paths.revision_key(self.storage_key, self.digest, 'hello.py'),
        ):
            with self.subTest(key=key):
                self.assertTrue(key.startswith(f'{paths.STORAGE_PREFIX}/'))

    def test_the_complete_object_key_budget_stays_inside_the_s3_ceiling(self):
        # S3 bounds the complete UTF-8 object key at 1024 bytes including every prefix. The
        # plugin prefix plus a maximum-length source path must leave room for an operator's
        # backend location, so the budget is pinned here where the pieces are defined.
        prefix = len(paths.revision_prefix(self.storage_key, self.digest).encode('utf-8'))
        self.assertLessEqual(prefix + paths.MAX_PATH_BYTES + 122, 1024)

    def test_a_file_key_is_canonicalized_however_the_path_was_spelled(self):
        expected = paths.revision_key(self.storage_key, self.digest, 'scripts/hello.py')
        for raw in ('./scripts/hello.py', 'scripts//hello.py', 'scripts/./hello.py'):
            with self.subTest(raw=raw):
                self.assertEqual(paths.revision_key(self.storage_key, self.digest, raw), expected)

    def test_a_file_key_refuses_a_path_that_would_leave_the_revision(self):
        for raw in ('../escape.py', '/etc/passwd', 'scripts/../../escape.py'):
            with self.subTest(raw=raw), self.assertRaises(UnsafePathError):
                paths.revision_key(self.storage_key, self.digest, raw)

    def test_the_key_builders_refuse_unsafe_identifiers(self):
        with self.assertRaises(UnsafePathError):
            paths.project_prefix('../etc')
        for bad_digest in ('../../escape', 'g' * 64, 'abc/def', 'A' * 64):
            with self.subTest(bad_digest=bad_digest), self.assertRaises(UnsafePathError):
                paths.revision_prefix(self.storage_key, bad_digest)

    def test_the_key_builders_are_pure_functions_of_their_arguments(self):
        self.assertEqual(
            paths.revision_key(self.storage_key, self.digest, 'scripts/hello.py'),
            paths.revision_key(self.storage_key, self.digest, 'scripts/hello.py'),
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

    def test_the_total_byte_limit_boundary_is_exact(self):
        segments = ['d' * 200, 'e' * 200, 'f' * 200]
        tail = 'x' * (paths.MAX_PATH_BYTES - sum(len(segment) + 1 for segment in segments) - 3) + '.py'
        path = '/'.join([*segments, tail])
        self.assertEqual(len(path.encode('utf-8')), paths.MAX_PATH_BYTES)
        self.assertEqual(paths.normalize_source_path(path), path)
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path(f'{path}x')
        self.assertEqual(ctx.exception.code, 'path_too_long')

    def test_rejects_a_path_over_the_depth_limit(self):
        deep = '/'.join(f'd{index}' for index in range(paths.MAX_PATH_DEPTH)) + '/mod.py'
        with self.assertRaises(UnsafePathError) as ctx:
            paths.normalize_source_path(deep)
        self.assertEqual(ctx.exception.code, 'path_too_deep')

    def test_accepts_a_path_at_the_depth_limit(self):
        deep = '/'.join(f'd{index}' for index in range(paths.MAX_PATH_DEPTH - 1)) + '/mod.py'
        self.assertEqual(paths.normalize_source_path(deep), deep)
