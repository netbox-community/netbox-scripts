import json

from django.test import TestCase

from netbox_scripts.models import ScriptFile, ScriptProject
from netbox_scripts.storage.exceptions import RevisionCorruptError
from netbox_scripts.storage.script_files import (
    EMPTY_SNAPSHOT_DIGEST,
    build_script_file_snapshot,
    compute_script_file_digest,
    validate_script_file_snapshot,
)


def entry(script_file, source_path):
    """Return one well-formed snapshot entry."""
    return {'script_file': script_file, 'source_path': source_path}


class BuildScriptFileSnapshotTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Snapshot Project', key='snapshot-project')

    def script_files(self, *paths):
        return [ScriptFile.objects.create(project=self.project, source_path=path) for path in paths]

    def test_snapshot_is_sorted_by_source_path(self):
        script_files = self.script_files('deploy.py', 'audit.py')
        snapshot, _ = build_script_file_snapshot(script_files)
        self.assertEqual([item['source_path'] for item in snapshot], ['audit.py', 'deploy.py'])
        self.assertEqual([item['script_file'] for item in snapshot], [script_files[1].pk, script_files[0].pk])

    def test_digest_is_independent_of_input_order(self):
        script_files = self.script_files('a.py', 'b.py')
        _, forward = build_script_file_snapshot(script_files)
        _, backward = build_script_file_snapshot(reversed(script_files))
        self.assertEqual(forward, backward)

    def test_the_empty_snapshot_digest_matches_the_constant(self):
        snapshot, digest = build_script_file_snapshot([])
        self.assertEqual(snapshot, [])
        self.assertEqual(digest, EMPTY_SNAPSHOT_DIGEST)

    def test_a_json_round_trip_keeps_the_digest(self):
        # The snapshot lives in a JSONField, so its digest must survive serialization.
        snapshot, digest = build_script_file_snapshot(self.script_files('a.py', 'b/c.py'))
        reloaded = json.loads(json.dumps(snapshot))
        self.assertEqual(compute_script_file_digest(reloaded), digest)

    def test_builder_output_validates(self):
        snapshot, digest = build_script_file_snapshot(self.script_files('a.py', 'b.py'))
        self.assertEqual(validate_script_file_snapshot(snapshot, digest), tuple(snapshot))


class ValidateScriptFileSnapshotTestCase(TestCase):
    """
    Cover the return trip, where a snapshot arrives from a database row rather than the builder.

    A stored snapshot decides which files validation imports and execution loads, so it is
    input again the moment it is read back.
    """

    def reasons_for(self, snapshot, digest=None):
        if digest is None:
            digest = compute_script_file_digest(snapshot) if isinstance(snapshot, list) else ''
        with self.assertRaises(RevisionCorruptError) as ctx:
            validate_script_file_snapshot(snapshot, digest)
        return ctx.exception.reasons

    def test_accepts_the_empty_snapshot(self):
        self.assertEqual(validate_script_file_snapshot([], EMPTY_SNAPSHOT_DIGEST), ())

    def test_rejects_a_snapshot_that_is_not_a_list(self):
        self.assertEqual(self.reasons_for({'script_file': 1}), ('snapshot_not_a_list',))

    def test_rejects_an_entry_that_is_not_a_mapping(self):
        self.assertEqual(self.reasons_for(['a.py']), ('malformed_entry:0',))

    def test_rejects_missing_fields(self):
        self.assertEqual(self.reasons_for([{'script_file': 1}]), ('missing_fields:0:source_path',))

    def test_rejects_unexpected_fields(self):
        snapshot = [{'script_file': 1, 'source_path': 'a.py', 'extra': True}]
        self.assertEqual(self.reasons_for(snapshot), ('unexpected_fields:0:extra',))

    def test_rejects_a_malformed_script_file_id(self):
        for script_file_id in (True, 0, -3, '7', None):
            with self.subTest(script_file_id=script_file_id):
                reasons = self.reasons_for([entry(script_file_id, 'a.py')])
                self.assertEqual(reasons, ('malformed_script_file_id:0',))

    def test_rejects_a_duplicate_script_file_id(self):
        snapshot = [entry(1, 'a.py'), entry(1, 'b.py')]
        self.assertEqual(self.reasons_for(snapshot), ('duplicate_script_file:1',))

    def test_rejects_a_path_that_is_not_a_string(self):
        self.assertEqual(self.reasons_for([entry(1, 42)]), ('malformed_path:0',))

    def test_rejects_an_unsafe_path(self):
        self.assertEqual(self.reasons_for([entry(1, '../outside.py')]), ('path_traversal:../outside.py',))

    def test_rejects_a_non_canonical_path(self):
        self.assertEqual(self.reasons_for([entry(1, 'pkg//mod.py')]), ('non_canonical_path:pkg//mod.py',))

    def test_rejects_an_unimportable_path(self):
        for path in ('my-tools/deploy.py', 'notes.txt', '__init__.py'):
            with self.subTest(path=path):
                self.assertEqual(self.reasons_for([entry(1, path)]), (f'unimportable_path:{path}',))

    def test_rejects_a_duplicate_path(self):
        snapshot = [entry(1, 'a.py'), entry(2, 'a.py')]
        self.assertEqual(self.reasons_for(snapshot), ('duplicate_path:a.py',))

    def test_rejects_a_case_fold_collision(self):
        snapshot = [entry(1, 'Utils.py'), entry(2, 'utils.py')]
        self.assertEqual(self.reasons_for(snapshot), ('case_fold_conflict:Utils.py', 'case_fold_conflict:utils.py'))

    def test_rejects_a_collision_in_an_ancestor_directory_only(self):
        snapshot = [entry(1, 'Lib/deploy.py'), entry(2, 'lib/audit.py')]
        self.assertEqual(
            self.reasons_for(snapshot),
            ('case_fold_conflict:Lib/deploy.py', 'case_fold_conflict:lib/audit.py'),
        )

    def test_allows_paths_only_full_caseless_folding_would_merge(self):
        snapshot = [entry(1, 'strasse.py'), entry(2, 'straße.py')]
        digest = compute_script_file_digest(snapshot)
        self.assertEqual(validate_script_file_snapshot(snapshot, digest), tuple(snapshot))

    def test_rejects_two_paths_importing_under_one_module_name(self):
        # Both import as "pkg", and the package wins, so "pkg.py" would never execute.
        snapshot = [entry(1, 'pkg.py'), entry(2, 'pkg/__init__.py')]
        self.assertEqual(
            self.reasons_for(snapshot),
            ('duplicate_module_name:pkg.py', 'duplicate_module_name:pkg/__init__.py'),
        )

    def test_rejects_an_unsorted_snapshot(self):
        snapshot = [entry(1, 'b.py'), entry(2, 'a.py')]
        self.assertEqual(self.reasons_for(snapshot), ('unsorted_entry:a.py',))

    def test_rejects_a_digest_that_does_not_address_the_snapshot(self):
        snapshot = [entry(1, 'a.py')]
        self.assertEqual(self.reasons_for(snapshot, 'b' * 64), ('script_file_digest_mismatch',))

    def test_skips_the_digest_check_when_the_entries_are_unusable(self):
        reasons = self.reasons_for([entry(1, '../outside.py')], 'b' * 64)
        self.assertNotIn('script_file_digest_mismatch', reasons)

    def test_reports_every_problem_at_once(self):
        snapshot = [entry(True, 'a.py'), entry(2, 'a.py'), entry(3, '../out.py')]
        self.assertEqual(
            self.reasons_for(snapshot),
            ('malformed_script_file_id:0', 'duplicate_path:a.py', 'path_traversal:../out.py'),
        )
