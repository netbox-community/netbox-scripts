import hashlib
import pathlib
import tempfile
import unicodedata

from django.test import TestCase, override_settings

from netbox_custom_scripts.storage import manifest, paths


def plugin_config(**settings):
    return {'netbox_custom_scripts': settings}


class ManifestTestCase(TestCase):
    def test_build_manifest_sorts_entries_by_path(self):
        entries, errors = manifest.build_manifest({'b.py': b'b', 'a.py': b'a', 'c/d.py': b'dd'})
        self.assertEqual(errors, [])
        self.assertEqual([entry['path'] for entry in entries], ['a.py', 'b.py', 'c/d.py'])
        self.assertEqual(entries[0], {'path': 'a.py', 'size': 1, 'sha256': hashlib.sha256(b'a').hexdigest()})

    def test_build_manifest_rejects_duplicate_normalized_paths(self):
        entries, errors = manifest.build_manifest({'scripts/a.py': b'1', './scripts/a.py': b'2'})
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['path'], 'scripts/a.py')
        self.assertEqual([error['code'] for error in errors], ['duplicate_path'])

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=4))
    def test_build_manifest_rejects_file_over_size_limit(self):
        entries, errors = manifest.build_manifest({'big.py': b'12345'})
        self.assertEqual(entries, [])
        self.assertEqual([error['code'] for error in errors], ['file_too_large'])
        self.assertEqual(errors[0]['path'], 'big.py')

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=5))
    def test_build_manifest_accepts_file_at_size_limit(self):
        entries, errors = manifest.build_manifest({'ok.py': b'12345'})
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['size'], 5)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_count=2))
    def test_build_manifest_rejects_project_over_file_count_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'a', 'b.py': b'b', 'c.py': b'c'})
        self.assertEqual(len(entries), 3)
        self.assertEqual([error['code'] for error in errors], ['too_many_files'])

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_count=3))
    def test_build_manifest_accepts_project_at_file_count_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'a', 'b.py': b'b', 'c.py': b'c'})
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 3)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_project_size=4))
    def test_build_manifest_rejects_project_over_total_size_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'12', 'b.py': b'345'})
        self.assertEqual(len(entries), 2)
        self.assertEqual([error['code'] for error in errors], ['project_too_large'])

    @override_settings(PLUGINS_CONFIG=plugin_config(max_project_size=5))
    def test_build_manifest_accepts_project_at_total_size_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'12', 'b.py': b'345'})
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_count=2))
    def test_build_manifest_counts_all_candidates_toward_file_count_limit(self):
        # One input is rejected (absolute path), but all three inputs count toward the limit.
        entries, errors = manifest.build_manifest({'a.py': b'a', 'b.py': b'b', '/bad.py': b'c'})
        self.assertEqual(sorted(error['code'] for error in errors), ['absolute_path', 'too_many_files'])
        self.assertEqual(len(entries), 2)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=4, max_project_size=5))
    def test_build_manifest_counts_rejected_files_toward_project_size(self):
        # big.py is rejected as too large but still counts toward the project total.
        entries, errors = manifest.build_manifest({'big.py': b'123456', 'small.py': b'1'})
        self.assertEqual(sorted(error['code'] for error in errors), ['file_too_large', 'project_too_large'])
        self.assertEqual(len(entries), 1)


class WalkerManifestBoundaryTestCase(TestCase):
    def test_normalization_collision_from_walker_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / unicodedata.normalize('NFC', 'café.py')).write_bytes(b'one')
            (root / unicodedata.normalize('NFD', 'café.py')).write_bytes(b'two')
            if len(list(root.iterdir())) < 2:
                self.skipTest('Filesystem normalizes Unicode filenames, so NFC and NFD collapse to one entry.')
            files = dict(paths.iter_directory_files(root))
            self.assertEqual(len(files), 2)
            entries, errors = manifest.build_manifest(files)
        self.assertEqual([error['code'] for error in errors], ['duplicate_path'])
        self.assertEqual(len(entries), 1)


class DigestTestCase(TestCase):
    def test_digest_independent_of_input_order(self):
        entries_one, _ = manifest.build_manifest({'a.py': b'a', 'b.py': b'b'})
        entries_two, _ = manifest.build_manifest({'b.py': b'b', 'a.py': b'a'})
        self.assertEqual(manifest.compute_digest(entries_one), manifest.compute_digest(entries_two))

    def test_digest_sensitive_to_content_change(self):
        base, _ = manifest.build_manifest({'a.py': b'a'})
        changed, _ = manifest.build_manifest({'a.py': b'A'})
        self.assertNotEqual(manifest.compute_digest(base), manifest.compute_digest(changed))

    def test_digest_sensitive_to_path_change(self):
        base, _ = manifest.build_manifest({'a.py': b'a'})
        moved, _ = manifest.build_manifest({'b.py': b'a'})
        self.assertNotEqual(manifest.compute_digest(base), manifest.compute_digest(moved))

    def test_digest_stable_across_object_key_reordering(self):
        entry_natural = {'path': 'a.py', 'size': 1, 'sha256': 'x'}
        entry_reordered = {'sha256': 'x', 'path': 'a.py', 'size': 1}
        self.assertEqual(manifest.compute_digest([entry_natural]), manifest.compute_digest([entry_reordered]))
