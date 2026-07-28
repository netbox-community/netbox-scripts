import hashlib
import unicodedata

from django.test import TestCase

from netbox_custom_scripts import constants
from netbox_custom_scripts.storage import manifest
from netbox_custom_scripts.storage.config import StorageLimits
from netbox_custom_scripts.storage.exceptions import RevisionCorruptError


def limits(**overrides):
    values = {
        'max_file_size': constants.DEFAULT_MAX_FILE_SIZE,
        'max_project_size': constants.DEFAULT_MAX_PROJECT_SIZE,
        'max_file_count': constants.DEFAULT_MAX_FILE_COUNT,
    }
    values.update(overrides)
    return StorageLimits(**values)


class ManifestTestCase(TestCase):
    def test_build_manifest_sorts_entries_by_path(self):
        entries, errors = manifest.build_manifest({'b.py': b'b', 'a.py': b'a', 'c/d.py': b'dd'}, limits())
        self.assertEqual(errors, [])
        self.assertEqual([entry['path'] for entry in entries], ['a.py', 'b.py', 'c/d.py'])
        self.assertEqual(entries[0], {'path': 'a.py', 'size': 1, 'sha256': hashlib.sha256(b'a').hexdigest()})

    def test_build_manifest_rejects_duplicate_normalized_paths(self):
        entries, errors = manifest.build_manifest({'scripts/a.py': b'1', './scripts/a.py': b'2'}, limits())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['path'], 'scripts/a.py')
        self.assertEqual([error['code'] for error in errors], ['duplicate_path'])

    def test_build_manifest_rejects_file_over_size_limit(self):
        entries, errors = manifest.build_manifest({'big.py': b'12345'}, limits(max_file_size=4))
        self.assertEqual(entries, [])
        self.assertEqual([error['code'] for error in errors], ['file_too_large'])
        self.assertEqual(errors[0]['path'], 'big.py')

    def test_build_manifest_accepts_file_at_size_limit(self):
        entries, errors = manifest.build_manifest({'ok.py': b'12345'}, limits(max_file_size=5))
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['size'], 5)

    def test_build_manifest_rejects_project_over_file_count_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'a', 'b.py': b'b', 'c.py': b'c'}, limits(max_file_count=2))
        self.assertEqual(len(entries), 3)
        self.assertEqual([error['code'] for error in errors], ['too_many_files'])

    def test_build_manifest_accepts_project_at_file_count_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'a', 'b.py': b'b', 'c.py': b'c'}, limits(max_file_count=3))
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 3)

    def test_build_manifest_rejects_project_over_total_size_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'12', 'b.py': b'345'}, limits(max_project_size=4))
        self.assertEqual(len(entries), 2)
        self.assertEqual([error['code'] for error in errors], ['project_too_large'])

    def test_build_manifest_accepts_project_at_total_size_limit(self):
        entries, errors = manifest.build_manifest({'a.py': b'12', 'b.py': b'345'}, limits(max_project_size=5))
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)

    def test_build_manifest_counts_all_candidates_toward_file_count_limit(self):
        # One input is rejected (absolute path), but all three inputs count toward the limit.
        entries, errors = manifest.build_manifest(
            {'a.py': b'a', 'b.py': b'b', '/bad.py': b'c'}, limits(max_file_count=2)
        )
        self.assertEqual(sorted(error['code'] for error in errors), ['absolute_path', 'too_many_files'])
        self.assertEqual(len(entries), 2)

    def test_build_manifest_counts_rejected_files_toward_project_size(self):
        # big.py is rejected as too large but still counts toward the project total.
        entries, errors = manifest.build_manifest(
            {'big.py': b'123456', 'small.py': b'1'}, limits(max_file_size=4, max_project_size=5)
        )
        self.assertEqual(sorted(error['code'] for error in errors), ['file_too_large', 'project_too_large'])
        self.assertEqual(len(entries), 1)

    def test_build_manifest_rejects_a_file_that_is_also_a_directory(self):
        # A source tree cannot hold both a file and a directory at "pkg", so this is a
        # content error rather than something for the filesystem writer to trip over.
        entries, errors = manifest.build_manifest({'pkg': b'file', 'pkg/module.py': b'child'}, limits())
        self.assertEqual([error['code'] for error in errors], ['path_conflict'])
        self.assertEqual(errors[0]['path'], 'pkg/module.py')
        self.assertIn('"pkg" is a file', errors[0]['message'])
        self.assertEqual([entry['path'] for entry in entries], ['pkg'])

    def test_build_manifest_rejects_the_conflict_regardless_of_input_order(self):
        for mapping in ({'pkg': b'file', 'pkg/m.py': b'child'}, {'pkg/m.py': b'child', 'pkg': b'file'}):
            with self.subTest(order=list(mapping)):
                entries, errors = manifest.build_manifest(mapping, limits())
                self.assertEqual([error['code'] for error in errors], ['path_conflict'])
                self.assertEqual([entry['path'] for entry in entries], ['pkg'])

    def test_build_manifest_rejects_a_nested_path_conflict(self):
        entries, errors = manifest.build_manifest({'pkg/sub': b'file', 'pkg/sub/module.py': b'child'}, limits())
        self.assertEqual([error['code'] for error in errors], ['path_conflict'])
        self.assertEqual([entry['path'] for entry in entries], ['pkg/sub'])

    def test_build_manifest_allows_a_directory_that_is_not_also_a_file(self):
        entries, errors = manifest.build_manifest({'pkg/__init__.py': b'', 'pkg/module.py': b'x'}, limits())
        self.assertEqual(errors, [])
        self.assertEqual([entry['path'] for entry in entries], ['pkg/__init__.py', 'pkg/module.py'])


class NormalizationCollisionTestCase(TestCase):
    def test_two_paths_differing_only_in_unicode_normalization_collide(self):
        # A source that carries both forms of one name reaches the manifest as two keys, and
        # only canonicalization can tell that they name one stored file.
        files = {
            unicodedata.normalize('NFC', 'café.py'): b'one',
            unicodedata.normalize('NFD', 'café.py'): b'two',
        }
        self.assertEqual(len(files), 2)
        entries, errors = manifest.build_manifest(files, limits())
        self.assertEqual([error['code'] for error in errors], ['duplicate_path'])
        self.assertEqual(len(entries), 1)


class CaseFoldCollisionTestCase(TestCase):
    """
    Case-fold collisions are content errors because accepted paths must materialize on
    every supported host, and hosts such as APFS treat "Utils.py" and "utils.py" as one
    file.
    """

    def test_build_manifest_rejects_two_files_differing_only_in_case(self):
        entries, errors = manifest.build_manifest({'Utils.py': b'a', 'utils.py': b'b'}, limits())
        self.assertEqual(entries, [])
        self.assertEqual([error['code'] for error in errors], ['case_fold_conflict', 'case_fold_conflict'])
        self.assertEqual(sorted(error['path'] for error in errors), ['Utils.py', 'utils.py'])
        self.assertIn('collide when letter case is ignored', errors[0]['message'])

    def test_build_manifest_rejects_paths_colliding_through_their_directories(self):
        entries, errors = manifest.build_manifest({'a/B.py': b'a', 'A/b.py': b'b'}, limits())
        self.assertEqual(entries, [])
        self.assertEqual(sorted(error['path'] for error in errors), ['A/b.py', 'a/B.py'])
        self.assertEqual({error['code'] for error in errors}, {'case_fold_conflict'})

    def test_build_manifest_rejects_a_file_colliding_with_a_case_folded_directory(self):
        entries, errors = manifest.build_manifest({'pkg': b'file', 'PKG/module.py': b'child'}, limits())
        self.assertEqual(entries, [])
        self.assertEqual(sorted(error['path'] for error in errors), ['PKG/module.py', 'pkg'])
        self.assertEqual({error['code'] for error in errors}, {'case_fold_conflict'})

    def test_build_manifest_allows_names_only_full_caseless_folding_would_merge(self):
        # The supported hosts apply simple case mapping, which keeps these two names apart.
        entries, errors = manifest.build_manifest({'straße.py': b'a', 'strasse.py': b'b'}, limits())
        self.assertEqual(errors, [])
        self.assertEqual([entry['path'] for entry in entries], ['strasse.py', 'straße.py'])

    def test_build_manifest_allows_distinct_names_sharing_one_directory(self):
        entries, errors = manifest.build_manifest({'pkg/a.py': b'a', 'pkg/b.py': b'b'}, limits())
        self.assertEqual(errors, [])
        self.assertEqual(len(entries), 2)

    def test_an_exact_file_directory_conflict_is_not_a_case_fold_conflict(self):
        # One exact name in both roles stays the plain path conflict from the check above.
        entries, errors = manifest.build_manifest({'pkg': b'file', 'pkg/module.py': b'child'}, limits())
        self.assertEqual([error['code'] for error in errors], ['path_conflict'])
        self.assertEqual([entry['path'] for entry in entries], ['pkg'])


class DigestTestCase(TestCase):
    def test_digest_independent_of_input_order(self):
        entries_one, _ = manifest.build_manifest({'a.py': b'a', 'b.py': b'b'}, limits())
        entries_two, _ = manifest.build_manifest({'b.py': b'b', 'a.py': b'a'}, limits())
        self.assertEqual(manifest.compute_digest(entries_one), manifest.compute_digest(entries_two))

    def test_digest_sensitive_to_content_change(self):
        base, _ = manifest.build_manifest({'a.py': b'a'}, limits())
        changed, _ = manifest.build_manifest({'a.py': b'A'}, limits())
        self.assertNotEqual(manifest.compute_digest(base), manifest.compute_digest(changed))

    def test_digest_sensitive_to_path_change(self):
        base, _ = manifest.build_manifest({'a.py': b'a'}, limits())
        moved, _ = manifest.build_manifest({'b.py': b'a'}, limits())
        self.assertNotEqual(manifest.compute_digest(base), manifest.compute_digest(moved))

    def test_digest_stable_across_object_key_reordering(self):
        entry_natural = {'path': 'a.py', 'size': 1, 'sha256': 'x'}
        entry_reordered = {'sha256': 'x', 'path': 'a.py', 'size': 1}
        self.assertEqual(manifest.compute_digest([entry_natural]), manifest.compute_digest([entry_reordered]))


def entry_for(path, content):
    """Return one well-formed manifest entry for a path and its content."""
    return {'path': path, 'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}


class ValidateManifestTestCase(TestCase):
    """
    Cover the return trip, where a manifest arrives from a database row rather than a builder.

    A stored manifest decides which files the storage layer opens, so it is input again the
    moment it is read back, and every one of these cases would otherwise reach an open call.
    """

    def test_accepts_a_manifest_the_builder_produced(self):
        entries, errors = manifest.build_manifest({'pkg/mod.py': b'body', 'top.py': b'xx'}, limits())
        self.assertEqual(errors, [])
        self.assertIsNone(manifest.validate_manifest(entries, manifest.compute_digest(entries)))

    def test_rejects_a_traversal_path(self):
        # ".." is a real directory entry, so O_NOFOLLOW opens it happily. This is the check
        # that keeps a stored manifest inside its own revision directory.
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('../outside.py', b'x')])
        self.assertEqual(ctx.exception.reasons, ('path_traversal:../outside.py',))

    def test_rejects_a_path_climbing_past_the_storage_root(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('../../../../etc/hostname', b'x')])
        self.assertEqual(ctx.exception.reasons, ('path_traversal:../../../../etc/hostname',))

    def test_rejects_an_absolute_path(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('/etc/hostname', b'x')])
        self.assertEqual(ctx.exception.reasons, ('absolute_path:/etc/hostname',))

    def test_rejects_a_non_canonical_path(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('pkg//mod.py', b'x')])
        self.assertEqual(ctx.exception.reasons, ('non_canonical_path:pkg//mod.py',))

    def test_rejects_duplicate_paths(self):
        # A dictionary keyed on path would silently keep whichever entry came last.
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('a.py', b'one'), entry_for('a.py', b'two')])
        self.assertEqual(ctx.exception.reasons, ('duplicate_path:a.py',))

    def test_rejects_a_file_and_directory_conflict(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('pkg', b'x'), entry_for('pkg/mod.py', b'y')])
        self.assertEqual(ctx.exception.reasons, ('path_conflict:pkg/mod.py',))

    def test_rejects_stored_paths_differing_only_in_case(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('Utils.py', b'a'), entry_for('utils.py', b'b')])
        self.assertEqual(ctx.exception.reasons, ('case_fold_conflict:Utils.py', 'case_fold_conflict:utils.py'))

    def test_rejects_a_stored_case_folded_ancestor_collision(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([entry_for('pkg', b'x'), entry_for('PKG/mod.py', b'y')])
        self.assertEqual(ctx.exception.reasons, ('case_fold_conflict:PKG/mod.py', 'case_fold_conflict:pkg'))

    def test_rejects_a_manifest_that_is_not_a_list(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest({'path': 'a.py'})
        self.assertEqual(ctx.exception.reasons, ('manifest_not_a_list',))

    def test_rejects_an_entry_that_is_not_a_mapping(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest(['a.py'])
        self.assertEqual(ctx.exception.reasons, ('malformed_entry:0',))

    def test_rejects_missing_fields_instead_of_raising_key_error(self):
        # Without this the storage layer leaks a raw KeyError to its caller.
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([{'path': 'a.py'}])
        self.assertEqual(ctx.exception.reasons, ('missing_fields:0:size,sha256',))

    def test_rejects_a_path_that_is_not_a_string(self):
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([{'path': 42, 'size': 1, 'sha256': 'a' * 64}])
        self.assertEqual(ctx.exception.reasons, ('malformed_path:0',))

    def test_rejects_a_malformed_size(self):
        for size in (-1, True, 1.5, '3'):
            with self.subTest(size=size), self.assertRaises(RevisionCorruptError) as ctx:
                manifest.validate_manifest([{'path': 'a.py', 'size': size, 'sha256': 'a' * 64}])
            self.assertEqual(ctx.exception.reasons, ('malformed_size:a.py',))

    def test_rejects_a_malformed_checksum(self):
        for checksum in ('A' * 64, 'a' * 63, 'z' * 64, 12345):
            with self.subTest(checksum=checksum), self.assertRaises(RevisionCorruptError) as ctx:
                manifest.validate_manifest([{'path': 'a.py', 'size': 1, 'sha256': checksum}])
            self.assertEqual(ctx.exception.reasons, ('malformed_checksum:a.py',))

    def test_rejects_a_digest_that_does_not_address_the_manifest(self):
        entries = [entry_for('a.py', b'aaa')]
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest(entries, 'b' * 64)
        self.assertEqual(ctx.exception.reasons, ('digest_mismatch',))

    def test_reports_every_problem_at_once(self):
        entries = [
            {'path': 'a.py', 'size': -1, 'sha256': 'nope'},
            entry_for('b.py', b'b'),
            entry_for('b.py', b'b'),
        ]
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest(entries)
        self.assertEqual(
            ctx.exception.reasons, ('malformed_size:a.py', 'malformed_checksum:a.py', 'duplicate_path:b.py')
        )

    def test_skips_the_digest_check_when_the_entries_are_unusable(self):
        # compute_digest reads entry['path'], so a malformed manifest must never reach it.
        with self.assertRaises(RevisionCorruptError) as ctx:
            manifest.validate_manifest([{'size': 1, 'sha256': 'a' * 64}], 'b' * 64)
        self.assertNotIn('digest_mismatch', ctx.exception.reasons)
