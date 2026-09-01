import errno
import fcntl
import os
import shutil
import stat
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import TestCase, override_settings

from netbox_custom_scripts.runtime import cache
from netbox_custom_scripts.runtime.exceptions import LocalCacheCorruptError, LocalCacheError
from netbox_custom_scripts.storage import store
from netbox_custom_scripts.storage.exceptions import RevisionCorruptError, StorageError, UnsafePathError
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.storage.paths import revision_key
from netbox_custom_scripts.tests.storage.test_backend_contract import HAS_S3_STACK
from netbox_custom_scripts.tests.storage.test_store import (
    STORAGE_KEY,
    OpenRecordingStorage,
    RefusingStorage,
    manifest_for,
)

if HAS_S3_STACK:
    import boto3
    from moto import mock_aws
    from storages.backends.s3 import S3Storage

FILES = {'deploy.py': b'print("deploy")\n', 'pkg/__init__.py': b'', 'pkg/util.py': b'VALUE = 1\n'}


def write_tree(root, files):
    """Lay a mapping of relative path to bytes down as real files under one root."""
    for path, content in files.items():
        destination = root / path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def tree_contents(root):
    """Return every regular file under one root as a mapping of relative path to bytes."""
    found = {}
    for base, _directories, files in Path(root).walk():
        for name in files:
            node = base / name
            found[node.relative_to(root).as_posix()] = node.read_bytes()
    return found


def discard_tree(root):
    """Remove one test tree, restoring the write bits publishing dropped."""
    if root.exists():
        root.chmod(0o755)
        for base, directories, _files in root.walk():
            for name in directories:
                (base / name).chmod(0o755)
    shutil.rmtree(root, ignore_errors=True)


@contextmanager
def refusing_read_only_rename():
    """Stand in for a host that checks write permission on the directory being renamed."""
    original = Path.rename

    def rename(self, target):
        info = self.lstat()
        if stat.S_ISDIR(info.st_mode) and not stat.S_IMODE(info.st_mode) & stat.S_IWUSR:
            raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(self))
        return original(self, target)

    with mock.patch.object(Path, 'rename', rename):
        yield


class GatedStorage(InMemoryStorage):
    """Serve open() only after the test allows it, holding a builder inside its critical section."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.entered = threading.Event()
        self.release = threading.Event()

    def open(self, name, mode='rb'):
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise TimeoutError('the test never released the gate')
        return super().open(name, mode)


class CacheRootTestCase(TestCase):
    """Cover where the cache root comes from and how revision directories are laid out."""

    def test_the_default_root_sits_under_the_system_temporary_directory(self):
        self.assertEqual(
            cache.resolve_cache_root(),
            Path(tempfile.gettempdir()) / 'netbox-custom-scripts' / 'runtime-cache',
        )

    @override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': '/var/cache/custom'}})
    def test_a_configured_root_wins_over_the_default(self):
        self.assertEqual(cache.resolve_cache_root(), Path('/var/cache/custom'))

    def test_a_revision_directory_nests_the_storage_key_and_digest_under_the_root(self):
        self.assertEqual(
            cache.local_revision_dir(STORAGE_KEY, 'a' * 64, '/somewhere'),
            Path('/somewhere') / str(STORAGE_KEY) / ('a' * 64),
        )

    def test_a_revision_directory_defaults_to_the_resolved_root(self):
        self.assertEqual(
            cache.local_revision_dir(STORAGE_KEY, 'b' * 64),
            cache.resolve_cache_root() / str(STORAGE_KEY) / ('b' * 64),
        )

    def test_a_component_that_cannot_name_a_storage_location_is_refused(self):
        # The local layout accepts exactly what the backend key layout accepts, so a value
        # that would escape one cannot be smuggled into the other.
        with self.assertRaises(UnsafePathError):
            cache.local_revision_dir('not-a-uuid', 'c' * 64, '/somewhere')
        with self.assertRaises(UnsafePathError):
            cache.local_revision_dir(STORAGE_KEY, '../escape', '/somewhere')

    def test_every_level_this_tier_creates_is_private(self):
        # Regression: mkdir(parents=True, mode=...) applies the mode to the leaf only, so an
        # intermediate directory was left at the process umask and the privacy check below then
        # rejected a root this tier had just created itself. Under a group-writable umask the
        # default root could therefore never be used.
        base = Path(tempfile.mkdtemp(prefix='nbcs-private-root-'))
        self.addCleanup(shutil.rmtree, base, True)
        root = base / 'netbox-custom-scripts' / 'runtime-cache'
        previous = os.umask(0o002)
        try:
            cache._ensure_private_root(root)
        finally:
            os.umask(previous)
        for directory in (root, root.parent):
            with self.subTest(directory=directory):
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_a_group_writable_ancestor_names_the_mode_fix(self):
        # The remedy is a mode change on the offending directory, so the message says so rather
        # than only suggesting a different root.
        base = Path(tempfile.mkdtemp(prefix='nbcs-open-root-'))
        self.addCleanup(shutil.rmtree, base, True)
        base.chmod(0o775)
        with self.assertRaises(LocalCacheError) as ctx:
            cache._ensure_private_root(base / 'runtime-cache')
        self.assertIn(f'chmod 700 {base}', str(ctx.exception))


class VerifyLocalTreeTestCase(TestCase):
    """Cover judging a local tree against a revision manifest."""

    files = FILES

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix='nbcs-verify-test-'))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.tree = self.root / 'tree'
        self.tree.mkdir()
        write_tree(self.tree, self.files)
        self.manifest = manifest_for(self.files)

    def assert_reasons(self, *expected):
        with self.assertRaises(LocalCacheCorruptError) as ctx:
            cache.verify_local_tree(self.tree, self.manifest)
        self.assertEqual(sorted(ctx.exception.reasons), sorted(expected))

    def test_an_intact_tree_verifies(self):
        cache.verify_local_tree(self.tree, self.manifest)

    def test_an_absent_tree_reports_missing_root(self):
        shutil.rmtree(self.tree)
        self.assert_reasons('missing_root')

    def test_a_root_that_is_only_a_symlink_reports_missing_root(self):
        actual = self.root / 'actual'
        shutil.move(self.tree, actual)
        self.tree.symlink_to(actual)
        self.assert_reasons('missing_root')

    def test_a_missing_file_is_reported(self):
        (self.tree / 'deploy.py').unlink()
        self.assert_reasons('missing:deploy.py')

    def test_a_size_mismatch_is_reported(self):
        (self.tree / 'deploy.py').write_bytes(self.files['deploy.py'] + b'tail')
        self.assert_reasons('size_mismatch:deploy.py')

    def test_a_checksum_mismatch_is_reported(self):
        (self.tree / 'deploy.py').write_bytes(b'X' * len(self.files['deploy.py']))
        self.assert_reasons('checksum_mismatch:deploy.py')

    def test_a_symlink_in_place_of_a_file_is_reported(self):
        # The link is never followed, so this fails on what it is, not on what it points at.
        (self.tree / 'deploy.py').unlink()
        (self.tree / 'deploy.py').symlink_to(self.root / 'actual')
        self.assert_reasons('symlink:deploy.py')

    def test_a_symlinked_directory_is_reported(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (self.tree / 'linked').symlink_to(elsewhere)
        self.assert_reasons('symlink:linked')

    def test_an_unexpected_file_is_reported(self):
        (self.tree / 'pkg' / 'sneaky.py').write_bytes(b'import os\n')
        self.assert_reasons('unexpected_entry:pkg/sneaky.py')

    def test_an_unexpected_directory_is_reported(self):
        (self.tree / 'stray').mkdir()
        self.assert_reasons('unexpected_entry:stray')

    def test_a_directory_squatting_on_a_file_path_is_reported(self):
        (self.tree / 'deploy.py').unlink()
        (self.tree / 'deploy.py').mkdir()
        self.assert_reasons('unexpected_entry:deploy.py')

    def test_a_compiled_file_gets_no_allowance(self):
        # Verification runs only after the purge, so anything compiled still on disk is a
        # finding, never an accepted artifact.
        pycache = self.tree / 'pkg' / '__pycache__'
        pycache.mkdir()
        (pycache / 'util.cpython-312.pyc').write_bytes(b'\x00')
        self.assert_reasons(
            'unexpected_entry:pkg/__pycache__',
            'unexpected_entry:pkg/__pycache__/util.cpython-312.pyc',
        )

    def test_every_problem_is_reported_at_once(self):
        (self.tree / 'deploy.py').unlink()
        (self.tree / 'pkg' / 'extra.txt').write_bytes(b'x')
        self.assert_reasons('missing:deploy.py', 'unexpected_entry:pkg/extra.txt')

    def test_an_empty_manifest_accepts_only_an_empty_tree(self):
        empty = self.root / 'empty'
        empty.mkdir()
        cache.verify_local_tree(empty, [])
        with self.assertRaises(LocalCacheCorruptError) as ctx:
            cache.verify_local_tree(self.tree, [])
        self.assertIn('unexpected_entry:deploy.py', ctx.exception.reasons)


class MaterializeRevisionTestCase(TestCase):
    """Cover settling one revision's cache slot against an authoritative store."""

    files = FILES

    def setUp(self):
        self.storage = InMemoryStorage()
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        store.write_revision(self.storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        self.cache_root = Path(tempfile.mkdtemp(prefix='nbcs-cache-test-'))
        self.addCleanup(discard_tree, self.cache_root)
        self.target = cache.local_revision_dir(STORAGE_KEY, self.digest, self.cache_root)

    def materialize(self, storage=None, manifest=None, digest=None, cache_root=None):
        return cache.materialize_revision(
            storage or self.storage,
            STORAGE_KEY,
            digest or self.digest,
            self.manifest if manifest is None else manifest,
            cache_root=cache_root or self.cache_root,
        )

    def unlock_target(self):
        """Restore write bits on the published tree, standing in for whatever damages it."""
        self.target.chmod(0o755)
        for base, directories, _files in self.target.walk():
            for name in directories:
                (base / name).chmod(0o755)

    def slot_residue(self):
        """Return the names sharing the slot's parent directory, the lock file aside."""
        return sorted(entry.name for entry in self.target.parent.iterdir() if entry.name != f'.{self.digest}.lock')

    def test_materializing_builds_a_verified_write_protected_tree(self):
        returned = self.materialize()
        self.assertEqual(returned, self.target)
        self.assertEqual(tree_contents(returned), self.files)
        cache.verify_local_tree(returned, self.manifest)
        self.assertEqual(stat.S_IMODE(returned.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((returned / 'pkg').stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((returned / 'deploy.py').stat().st_mode), 0o444)
        with self.assertRaises(OSError):
            (returned / 'planted.py').write_bytes(b'nope')

    def test_publishing_survives_a_host_that_refuses_to_rename_a_read_only_directory(self):
        with refusing_read_only_rename():
            returned = self.materialize()
        self.assertEqual(returned, self.target)
        cache.verify_local_tree(returned, self.manifest)
        self.assertEqual(stat.S_IMODE(returned.stat().st_mode), 0o555)

    def test_a_verified_tree_is_served_again_without_any_backend_traffic(self):
        self.materialize()
        # The second pass gets a backend holding nothing at all, so anything it serves can
        # only have come from the verified tree already on disk.
        watcher = OpenRecordingStorage()
        self.assertEqual(self.materialize(storage=watcher), self.target)
        self.assertEqual(watcher.opened, [])

    def test_a_writable_published_tree_is_protected_again_before_it_is_served(self):
        # What a build interrupted between its rename and its write-protect leaves behind.
        self.materialize()
        self.unlock_target()
        watcher = OpenRecordingStorage()
        returned = self.materialize(storage=watcher)
        # No backend traffic, so this is the serve path repairing the modes rather than a rebuild.
        self.assertEqual(watcher.opened, [])
        self.assertEqual(stat.S_IMODE(returned.stat().st_mode), 0o555)
        self.assertEqual(stat.S_IMODE((returned / 'pkg').stat().st_mode), 0o555)

    def test_a_damaged_tree_is_set_aside_and_rebuilt(self):
        self.materialize()
        self.unlock_target()
        (self.target / 'deploy.py').chmod(0o644)
        (self.target / 'deploy.py').write_bytes(b'tampered content here')
        self.assertEqual(self.materialize(), self.target)
        cache.verify_local_tree(self.target, self.manifest)
        aside = [entry for entry in self.target.parent.iterdir() if entry.name.startswith(f'{self.digest}.corrupt.')]
        self.assertEqual(len(aside), 1)
        # The failed tree is evidence, set aside intact rather than destroyed.
        self.assertEqual((aside[0] / 'deploy.py').read_bytes(), b'tampered content here')

    def test_setting_a_failed_tree_aside_survives_the_same_refusal(self):
        self.materialize()
        # The file's own mode, not its directory's, so the slot root stays write-protected and the
        # aside rename has to move a 0o555 directory.
        tampered = self.target / 'deploy.py'
        tampered.chmod(0o644)
        tampered.write_bytes(b'tampered content here')
        with refusing_read_only_rename():
            self.assertEqual(self.materialize(), self.target)
        cache.verify_local_tree(self.target, self.manifest)
        aside = [entry for entry in self.target.parent.iterdir() if entry.name.startswith(f'{self.digest}.corrupt.')]
        self.assertEqual(len(aside), 1)
        self.assertEqual((aside[0] / 'deploy.py').read_bytes(), b'tampered content here')
        # The lent bit stays, since every directory under it is still read-only anyway.
        self.assertEqual(stat.S_IMODE(aside[0].stat().st_mode), 0o755)

    def test_pre_existing_bytecode_is_purged_and_never_trusted(self):
        self.materialize()
        self.unlock_target()
        pycache = self.target / 'pkg' / '__pycache__'
        pycache.mkdir()
        (pycache / 'util.cpython-312.pyc').write_bytes(b'planted')
        (self.target / 'stray.pyc').write_bytes(b'planted')
        watcher = OpenRecordingStorage()
        self.assertEqual(self.materialize(storage=watcher), self.target)
        # No backend traffic, so the purge alone reconciled the tree before verification,
        # and the plants are gone rather than tolerated.
        self.assertEqual(watcher.opened, [])
        self.assertEqual(tree_contents(self.target), self.files)

    def test_pre_existing_bytecode_is_purged_whatever_its_letter_case(self):
        self.materialize()
        self.unlock_target()
        pycache = self.target / 'pkg' / '__PyCache__'
        pycache.mkdir()
        (pycache / 'util.cpython-312.PYC').write_bytes(b'planted')
        (self.target / 'stray.PYC').write_bytes(b'planted')
        watcher = OpenRecordingStorage()
        self.assertEqual(self.materialize(storage=watcher), self.target)
        # No backend traffic, so the purge reconciled the tree rather than a rebuild doing it.
        self.assertEqual(watcher.opened, [])
        self.assertEqual(tree_contents(self.target), self.files)

    @unittest.skipIf(os.geteuid() == 0, 'write protection does not bind the superuser')
    def test_bytecode_that_cannot_be_removed_fails_closed(self):
        self.materialize()
        self.unlock_target()
        pycache = self.target / 'pkg' / '__pycache__'
        pycache.mkdir()
        (pycache / 'util.cpython-312.pyc').write_bytes(b'planted')
        pycache.chmod(0o555)
        with self.assertRaises(LocalCacheError) as ctx:
            self.materialize()
        self.assertIn('compiled artifacts', str(ctx.exception))

    def test_a_symlink_smuggled_into_the_slot_forces_a_rebuild(self):
        self.materialize()
        self.unlock_target()
        (self.target / 'deploy.py').unlink()
        (self.target / 'deploy.py').symlink_to('/etc/hostname')
        self.assertEqual(self.materialize(), self.target)
        self.assertFalse((self.target / 'deploy.py').is_symlink())
        cache.verify_local_tree(self.target, self.manifest)

    def test_a_non_directory_occupying_the_slot_is_moved_aside_without_a_chmod(self):
        # The write bit is lent to a directory only. chmod follows a symlink, so lending it to one
        # would change the mode of whatever it points at.
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(b'X = 1\n')
        self.target.chmod(0o444)
        self.assertEqual(self.materialize(), self.target)
        cache.verify_local_tree(self.target, self.manifest)
        aside = [entry for entry in self.target.parent.iterdir() if entry.name.startswith(f'{self.digest}.corrupt.')]
        self.assertEqual(len(aside), 1)
        self.assertEqual(stat.S_IMODE(aside[0].stat().st_mode), 0o444)

    def test_an_unexpected_file_in_the_slot_forces_a_rebuild(self):
        self.materialize()
        self.unlock_target()
        (self.target / 'pkg' / 'sneaky.py').write_bytes(b'import os\n')
        self.assertEqual(self.materialize(), self.target)
        self.assertEqual(tree_contents(self.target), self.files)

    def test_an_empty_revision_materializes_as_an_empty_read_only_directory(self):
        digest = compute_digest([])
        target = cache.materialize_revision(InMemoryStorage(), STORAGE_KEY, digest, [], cache_root=self.cache_root)
        self.assertEqual(target, cache.local_revision_dir(STORAGE_KEY, digest, self.cache_root))
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o555)

    def test_a_cache_root_too_deep_for_the_revision_is_refused(self):
        deep = Path('/', *(['x' * 200] * 20))
        with self.assertRaises(LocalCacheError) as ctx:
            self.materialize(cache_root=deep)
        self.assertIn('byte budget', str(ctx.exception))

    def test_a_backend_failure_surfaces_and_publishes_nothing(self):
        failing = RefusingStorage(failing=set())
        store.write_revision(failing, STORAGE_KEY, self.digest, self.files, self.manifest)
        failing.failing = {'open'}
        with self.assertRaises(StorageError) as ctx:
            self.materialize(storage=failing)
        self.assertNotIsInstance(ctx.exception, LocalCacheError)
        self.assertEqual(self.slot_residue(), [])

    def test_authoritative_corruption_surfaces_and_publishes_nothing(self):
        key = revision_key(STORAGE_KEY, self.digest, 'deploy.py')
        self.storage.delete(key)
        self.storage.save(key, ContentFile(b'X' * len(self.files['deploy.py'])))
        with self.assertRaises(RevisionCorruptError) as ctx:
            self.materialize()
        self.assertEqual(sorted(ctx.exception.reasons), ['checksum_mismatch:deploy.py'])
        self.assertEqual(self.slot_residue(), [])

    def test_a_tree_that_cannot_be_protected_is_not_served(self):
        with (
            mock.patch.object(cache, '_make_read_only', side_effect=LocalCacheError('chmod refused')),
            self.assertRaises(LocalCacheError),
        ):
            self.materialize()
        watcher = OpenRecordingStorage()
        returned = self.materialize(storage=watcher)
        # The published tree verifies, so the next call protects it where it stands.
        self.assertEqual(watcher.opened, [])
        self.assertEqual(stat.S_IMODE(returned.stat().st_mode), 0o555)

    def test_a_tampered_manifest_is_refused_before_anything_is_written(self):
        tampered = [*self.manifest, {'path': '../escape.py', 'size': 1, 'sha256': 'a' * 64}]
        with self.assertRaises(RevisionCorruptError):
            self.materialize(manifest=tampered)
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_losing_the_publish_race_to_a_good_tree_still_succeeds(self):
        # The slot is already settled, and the builder is blinded to that so its own rename
        # loses. A lost race against a verified tree is a success, not a failure.
        self.materialize()
        with mock.patch.object(cache, '_existing_tree_verifies', return_value=False):
            self.assertEqual(self.materialize(), self.target)
        cache.verify_local_tree(self.target, self.manifest)
        self.assertEqual(stat.S_IMODE(self.target.stat().st_mode), 0o555)

    def test_the_slot_lock_is_held_for_the_whole_critical_section(self):
        gated = GatedStorage()
        gated.release.set()
        store.write_revision(gated, STORAGE_KEY, self.digest, self.files, self.manifest)
        gated.release.clear()
        gated.entered.clear()

        outcome = {}

        def build():
            try:
                outcome['dir'] = self.materialize(storage=gated)
            except Exception as error:
                outcome['error'] = error

        lock_path = self.target.parent / f'.{self.digest}.lock'
        worker = threading.Thread(target=build)
        worker.start()
        try:
            # The builder is parked on its first backend read, inside the critical section,
            # so the slot lock must refuse a second holder right now.
            self.assertTrue(gated.entered.wait(timeout=10))
            with lock_path.open('ab') as probe, self.assertRaises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            gated.release.set()
            worker.join(timeout=10)
        self.assertFalse(worker.is_alive())
        error = outcome.get('error')
        self.assertIsNone(error, msg=repr(error))
        self.assertEqual(outcome.get('dir'), self.target)
        with lock_path.open('ab') as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
        cache.verify_local_tree(self.target, self.manifest)

    def test_a_staging_tree_is_removed_when_publishing_fails(self):
        # Staging stays writable until the rename, so cleanup needs no permission recovery, and a
        # cleanup failure must not replace the error being reported.
        with mock.patch.object(cache, '_publish', side_effect=OSError('publish refused')), self.assertRaises(OSError):
            self.materialize()
        self.assertEqual(self.slot_residue(), [])

    @unittest.skipIf(os.getuid() == 0, 'root ignores directory permissions')
    def test_an_unreadable_subdirectory_is_a_cache_error_not_missing_content(self):
        # Skipping the subtree silently would report every file under it as missing, turning a
        # permission fault into a corruption verdict and three needless rebuilds.
        self.materialize()
        self.unlock_target()
        blocked = self.target / 'pkg'
        blocked.chmod(0o000)
        self.addCleanup(blocked.chmod, 0o755)
        with self.assertRaises(LocalCacheError) as caught:
            cache.verify_local_tree(self.target, self.manifest)
        self.assertNotIsInstance(caught.exception, LocalCacheCorruptError)

    def test_an_ancestor_owned_by_another_user_is_refused(self):
        with (
            mock.patch.object(cache.os, 'getuid', return_value=os.getuid() + 1),
            self.assertRaises(LocalCacheError) as caught,
        ):
            self.materialize()
        self.assertIn('belongs to another user', str(caught.exception))

    def test_a_world_writable_ancestor_is_refused_unless_it_is_sticky(self):
        base = Path(tempfile.mkdtemp(prefix='nbcs-root-test-'))
        self.addCleanup(discard_tree, base)
        base.chmod(0o777)
        with self.assertRaises(LocalCacheError) as caught:
            self.materialize(cache_root=base / 'runtime-cache')
        self.assertIn('writable by other users', str(caught.exception))

    def test_a_sticky_world_writable_ancestor_is_accepted(self):
        # This is what keeps the default root under the shared temporary directory usable.
        base = Path(tempfile.mkdtemp(prefix='nbcs-root-test-'))
        self.addCleanup(discard_tree, base)
        base.chmod(0o1777)
        returned = self.materialize(cache_root=base / 'runtime-cache')
        cache.verify_local_tree(returned, self.manifest)

    def test_the_cache_root_is_created_private_to_this_user(self):
        base = Path(tempfile.mkdtemp(prefix='nbcs-root-test-'))
        self.addCleanup(discard_tree, base)
        root = base / 'runtime-cache'
        self.materialize(cache_root=root)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode) & 0o077, 0)


@unittest.skipUnless(HAS_S3_STACK, 'moto and the S3 storage backend are not installed')
class S3MaterializationTestCase(TestCase):
    """Materialization against an S3-compatible object store, mocked by moto."""

    files = FILES

    def setUp(self):
        self.enterContext(mock_aws())
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='netbox-custom-scripts-cache-test')
        self.storage = S3Storage(
            bucket_name='netbox-custom-scripts-cache-test',
            region_name='us-east-1',
            access_key='testing',
            secret_key='testing',
        )
        self.manifest = manifest_for(self.files)
        self.digest = compute_digest(self.manifest)
        store.write_revision(self.storage, STORAGE_KEY, self.digest, self.files, self.manifest)
        self.cache_root = Path(tempfile.mkdtemp(prefix='nbcs-s3-cache-test-'))
        self.addCleanup(discard_tree, self.cache_root)

    def test_materialization_pulls_a_verified_tree_from_object_storage(self):
        target = cache.materialize_revision(
            self.storage, STORAGE_KEY, self.digest, self.manifest, cache_root=self.cache_root
        )
        cache.verify_local_tree(target, self.manifest)
        self.assertEqual(tree_contents(target), self.files)

    def test_a_wrong_sized_object_is_rejected_without_opening_its_body(self):
        # This backend downloads an object whole when it is opened, so a wrong-size
        # replacement has to be refused from its metadata, before any open happens.
        key = revision_key(STORAGE_KEY, self.digest, 'deploy.py')
        self.storage.delete(key)
        self.storage.save(key, ContentFile(self.files['deploy.py'] * 4096))
        opened = []
        original = self.storage.open
        with (
            mock.patch.object(
                self.storage, 'open', side_effect=lambda name, mode='rb': opened.append(name) or original(name, mode)
            ),
            self.assertRaises(RevisionCorruptError) as ctx,
        ):
            cache.materialize_revision(
                self.storage, STORAGE_KEY, self.digest, self.manifest, cache_root=self.cache_root
            )
        self.assertEqual(sorted(ctx.exception.reasons), ['size_mismatch:deploy.py'])
        self.assertNotIn(key, opened)
