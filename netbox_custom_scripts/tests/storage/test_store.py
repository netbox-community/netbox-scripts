import contextlib
import hashlib
import os
import pathlib
import shutil
import tempfile
import uuid
from unittest import mock

from django.test import TestCase

from netbox_custom_scripts import constants
from netbox_custom_scripts.storage import store
from netbox_custom_scripts.storage.config import StorageLimits
from netbox_custom_scripts.storage.exceptions import (
    LimitExceededError,
    RevisionCorruptError,
    StorageError,
    UnsafePathError,
)
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.storage.paths import MAX_PATH_DEPTH, normalize_source_path


def limits(**overrides):
    values = {
        'max_file_size': constants.DEFAULT_MAX_FILE_SIZE,
        'max_project_size': constants.DEFAULT_MAX_PROJECT_SIZE,
        'max_file_count': constants.DEFAULT_MAX_FILE_COUNT,
    }
    values.update(overrides)
    return StorageLimits(**values)


class UninspectableEntry:
    """
    A directory entry whose kind cannot be determined, which os.scandir permits.

    scandir answers is_dir and is_file from the listing when the filesystem supplies the entry
    type and falls back to a stat call when it does not, so an entry removed or made
    inaccessible after the listing raises from the question rather than from the enumeration.
    """

    def __init__(self, name, error=None):
        self.name = name
        self._error = error or PermissionError('denied')

    def is_symlink(self):
        return False

    def is_dir(self, follow_symlinks=True):
        raise self._error

    def is_file(self, follow_symlinks=True):
        raise self._error


def scandir_returning(*entries):
    """Stand in for os.scandir, which the walkers use as a context manager and iterate once."""

    @contextlib.contextmanager
    def scandir(dir_fd):
        yield list(entries)

    return scandir


def refusing_to_remove(name):
    """Stand in for shutil.rmtree, refusing one directory name and passing everything else."""
    real = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if path == name:
            raise PermissionError('denied')
        return real(path, *args, **kwargs)

    return rmtree


def refusing_to_quarantine():
    """Stand in for os.rename, refusing only the rename that sets a revision aside."""
    real = os.rename

    def rename(source, target, *args, **kwargs):
        if store._QUARANTINE_SUFFIX in str(target):
            raise PermissionError('denied')
        return real(source, target, *args, **kwargs)

    return rename


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

    def test_iter_directory_files_converts_listing_errors_to_storage_error(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.store.os.scandir', side_effect=PermissionError('denied')),
            self.assertRaises(StorageError),
        ):
            list(store.iter_directory_files(tmp, limits()))

    def test_iter_directory_files_converts_entry_metadata_errors_to_storage_error(self):
        # Classifying an entry is as much a filesystem operation as opening it, so it belongs
        # on the same boundary rather than escaping as a bare PermissionError.
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(store.os, 'scandir', scandir_returning(UninspectableEntry('a.py'))),
            self.assertRaises(StorageError) as ctx,
        ):
            list(store.iter_directory_files(tmp, limits()))
        self.assertIn('a.py', str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, PermissionError)

    def test_iter_directory_files_converts_file_read_errors_to_storage_error(self):
        real_open = os.open

        def refuse_files(path, flags, *args, **kwargs):
            # Let the directory descriptors through, so only the file read fails.
            if flags & os.O_DIRECTORY:
                return real_open(path, flags, *args, **kwargs)
            raise PermissionError('denied')

        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / 'a.py').write_bytes(b'x')
            with (
                mock.patch('netbox_custom_scripts.storage.store.os.open', refuse_files),
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

    def test_iter_directory_files_refuses_a_directory_swapped_mid_walk(self):
        # The window is inside one directory's file list, not between directory levels: a
        # swap before descent is caught by the walk itself, so the obvious version of this
        # test passes even against a pathname-based walker. Suspending the generator after
        # the first file of a directory is what exposes the difference.
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'root'
            (root / 'pkg').mkdir(parents=True)
            (root / 'pkg' / 'a_first.py').write_bytes(b'legit one')
            (root / 'pkg' / 'b_second.py').write_bytes(b'legit two')
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'b_second.py').write_bytes(b'secret from outside the root')

            walker = store.iter_directory_files(root, limits())
            self.assertEqual(next(walker), ('pkg/a_first.py', b'legit one'))
            shutil.rmtree(root / 'pkg')
            (root / 'pkg').symlink_to(outside, target_is_directory=True)

            # The property under test is that nothing outside the root is ever yielded. The
            # swapped-away file now reads as missing against the real descriptor, so the walk
            # raises rather than serving the impostor, but either outcome must not leak.
            leaked = []
            with contextlib.suppress(StorageError):
                leaked = [body for _relative, body in walker]
            self.assertNotIn(b'secret from outside the root', leaked)

    def test_iter_directory_files_refuses_a_tree_over_the_depth_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'root'
            deep = root.joinpath(*[f'd{index}' for index in range(MAX_PATH_DEPTH + 1)])
            deep.mkdir(parents=True)
            (deep / 'mod.py').write_bytes(b'x')
            with self.assertRaises(UnsafePathError) as ctx:
                dict(store.iter_directory_files(root, limits()))
            self.assertEqual(ctx.exception.code, 'path_too_deep')


STORAGE_KEY = uuid.UUID('11111111-2222-3333-4444-555555555555')
# A revision directory is named by the digest of its manifest, and the store now enforces
# that pairing, so a fixture derives the name from the content instead of asserting one.
EMPTY_DIGEST = compute_digest([])
# Where a digest is only a directory name and no manifest is involved, any well-formed value
# does the job.
PLACEHOLDER_DIGEST = 'a' * 64


def manifest_for(files):
    """Return manifest entries for a source mapping, canonicalized as build_manifest would."""
    return sorted(
        (
            {'path': normalize_source_path(path), 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest()}
            for path, body in files.items()
        ),
        key=lambda entry: entry['path'],
    )


def digest_for(files):
    """Return the content address of a source mapping, the one the service would store."""
    return compute_digest(manifest_for(files))


class VerifyRevisionTreeTestCase(TestCase):
    def test_verify_accepts_a_matching_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa', 'pkg/b.py': b'bbbb'}
            store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            root = store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertTrue(root.is_dir())

    def test_verify_converts_entry_metadata_errors_to_storage_error(self):
        # The verifier walks whatever is on disk, so it meets the same raced entry the source
        # walker does and reports it the same way.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            with (
                mock.patch.object(store.os, 'scandir', scandir_returning(UninspectableEntry('a.py'))),
                self.assertRaises(StorageError) as ctx,
            ):
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
        self.assertIn('a.py', str(ctx.exception))
        self.assertNotIsInstance(ctx.exception, RevisionCorruptError)

    def test_verify_rejects_a_missing_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('root_missing', ctx.exception.reasons)

    def test_verify_rejects_a_symlinked_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / 'real'
            real.mkdir()
            link = store.revision_directory(tmp, STORAGE_KEY, EMPTY_DIGEST)
            link.parent.mkdir(parents=True)
            link.symlink_to(real, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, EMPTY_DIGEST, [])
            self.assertIn('root_symlink', ctx.exception.reasons)

    def test_verify_rejects_a_symlinked_project_directory(self):
        # A link one level above the revision is just as good an escape as a link on the
        # revision itself, and neither the final component nor its parent is a link here.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            digest = digest_for(files)
            outside = pathlib.Path(tmp) / 'outside'
            (outside / 'revisions' / digest).mkdir(parents=True)
            (outside / 'revisions' / digest / 'a.py').write_bytes(b'aaa')
            (pathlib.Path(tmp) / 'root').mkdir()
            (pathlib.Path(tmp) / 'root' / str(STORAGE_KEY)).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(pathlib.Path(tmp) / 'root', STORAGE_KEY, digest, manifest_for(files))
            self.assertIn('root_symlink', ctx.exception.reasons)

    def test_verify_rejects_a_symlinked_revisions_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = pathlib.Path(tmp) / 'outside'
            (outside / EMPTY_DIGEST).mkdir(parents=True)
            root = pathlib.Path(tmp) / 'root'
            (root / str(STORAGE_KEY)).mkdir(parents=True)
            (root / str(STORAGE_KEY) / 'revisions').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(root, STORAGE_KEY, EMPTY_DIGEST, [])
            self.assertIn('root_symlink', ctx.exception.reasons)

    def test_verify_rejects_a_plain_file_as_the_revision_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp) / 'root'
            (root / str(STORAGE_KEY) / 'revisions').mkdir(parents=True)
            (root / str(STORAGE_KEY) / 'revisions' / EMPTY_DIGEST).write_bytes(b'not a directory')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(root, STORAGE_KEY, EMPTY_DIGEST, [])
            self.assertIn('root_not_directory', ctx.exception.reasons)

    def test_verify_rejects_a_file_beneath_an_intermediate_symlink(self):
        # The manifest entry itself is not a link, so a check that only looks at the final
        # component accepts content served from outside the revision.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'pkg/mod.py': b'body'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'mod.py').write_bytes(b'body')
            store.delete_revision_directory(tmp, STORAGE_KEY, digest_for(files))
            destination.mkdir(parents=True)
            (destination / 'pkg').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('symlink:pkg/mod.py', ctx.exception.reasons)

    def test_verify_rejects_an_unexpected_empty_directory(self):
        # An unexpected directory is importable content: it can act as a namespace package.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'smuggled').mkdir()
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('unexpected_directory:smuggled', ctx.exception.reasons)

    def test_verify_rejects_an_unexpected_symlinked_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'extra.py').write_bytes(b'x')
            (destination / 'smuggled').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('unexpected_symlink:smuggled', ctx.exception.reasons)

    def test_verify_accepts_the_directories_its_manifest_implies(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'pkg/deep/mod.py': b'body', 'top.py': b'x'}
            store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            root = store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertTrue((root / 'pkg' / 'deep' / 'mod.py').is_file())

    def test_verify_rejects_a_parent_directory_replaced_by_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'pkg/mod.py': b'body'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'mod.py').write_bytes(b'body')
            (destination / 'pkg' / 'mod.py').unlink()
            (destination / 'pkg').rmdir()
            (destination / 'pkg').symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('symlink:pkg/mod.py', ctx.exception.reasons)

    def test_verify_rejects_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').unlink()
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('missing_or_special:a.py', ctx.exception.reasons)

    def test_verify_rejects_a_size_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').write_bytes(b'aaaa')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('size_mismatch:a.py', ctx.exception.reasons)

    def test_verify_rejects_a_checksum_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').write_bytes(b'bbb')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('checksum_mismatch:a.py', ctx.exception.reasons)

    def test_verify_rejects_an_unexpected_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'extra.py').write_bytes(b'smuggled')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertIn('unexpected_file:extra.py', ctx.exception.reasons)

    def test_verify_reports_every_mismatch_at_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa', 'b.py': b'bbb'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').unlink()
            (destination / 'b.py').write_bytes(b'ccc')
            (destination / 'extra.py').write_bytes(b'x')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for(files))
            self.assertEqual(len(ctx.exception.reasons), 3)

    def test_verify_rejects_a_manifest_path_leaving_the_revision_directory(self):
        # ".." is a real directory entry rather than a link, so descriptor-relative traversal
        # does not stop it. A stored manifest is input too, and this is where it is treated
        # as one.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            digest = digest_for(files)
            store.write_staged_revision(tmp, STORAGE_KEY, digest, files, manifest_for(files))
            outside = pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions' / 'outside.py'
            outside.write_bytes(b'outside')
            poisoned = [{'path': '../outside.py', 'size': 7, 'sha256': hashlib.sha256(b'outside').hexdigest()}]
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest, poisoned)
            self.assertEqual(ctx.exception.reasons, ('path_traversal:../outside.py',))
            self.assertTrue(outside.exists())

    def test_verify_rejects_a_digest_that_does_not_address_its_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.verify_revision_tree(tmp, STORAGE_KEY, digest_for(files), manifest_for({'b.py': b'bbb'}))
            self.assertEqual(ctx.exception.reasons, ('digest_mismatch',))


class RevisionStoreTestCase(TestCase):
    def test_write_staged_revision_creates_revision_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            project = pathlib.Path(tmp) / str(STORAGE_KEY)
            self.assertEqual(destination, project / 'revisions' / digest_for(files))
            self.assertEqual((destination / 'a.py').read_bytes(), b'aaa')
            self.assertEqual(list((project / 'staging').iterdir()), [])

    def test_write_staged_revision_writes_nested_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'pkg/__init__.py': b'', 'pkg/deep/mod.py': b'body'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual((destination / 'pkg' / '__init__.py').read_bytes(), b'')
            self.assertEqual((destination / 'pkg' / 'deep' / 'mod.py').read_bytes(), b'body')

    def test_write_staged_revision_canonicalizes_source_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'./pkg//mod.py': b'body'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual((destination / 'pkg' / 'mod.py').read_bytes(), b'body')

    def test_write_staged_revision_rejects_duplicate_canonical_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'mod.py': b'first', './mod.py': b'second'}
            with self.assertRaises(UnsafePathError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, EMPTY_DIGEST, files, [])
            self.assertEqual(ctx.exception.code, 'duplicate_path')
            self.assertFalse((pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions' / EMPTY_DIGEST).exists())

    def test_write_staged_revision_is_idempotent_when_destination_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'first'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            again = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual(again, destination)
            self.assertEqual((destination / 'a.py').read_bytes(), b'first')

    def test_write_staged_revision_rejects_an_existing_destination_that_does_not_match(self):
        # A digest addresses its content, so a destination holding different bytes is damaged
        # rather than equivalent. Reusing it silently would hide tampering.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'first'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').write_bytes(b'tampered')
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertTrue(
                any(reason.startswith(('size_mismatch', 'checksum_mismatch')) for reason in ctx.exception.reasons)
            )

    def test_write_staged_revision_rejects_content_that_does_not_match_its_manifest(self):
        # The postcondition is that the stored tree matches the manifest, so it is checked
        # rather than assumed even on the ordinary write path.
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.write_staged_revision(
                    tmp,
                    STORAGE_KEY,
                    digest_for({'a.py': b'promised'}),
                    {'a.py': b'written'},
                    manifest_for({'a.py': b'promised'}),
                )
            self.assertTrue(
                any(reason.startswith(('size_mismatch', 'checksum_mismatch')) for reason in ctx.exception.reasons)
            )
            project = pathlib.Path(tmp) / str(STORAGE_KEY)
            # A successful rename consumes the staging token, so without withdrawing the
            # destination this call installed, the bad tree would stay installed for good.
            self.assertFalse((project / 'revisions' / digest_for({'a.py': b'promised'})).exists())
            self.assertEqual(list((project / 'staging').iterdir()), [])

    def test_write_staged_revision_can_be_retried_after_a_verification_failure(self):
        # The documented transition is storage_failed -> staging -> materialized, which only
        # holds if the failed attempt leaves nothing installed under the digest.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'promised'}
            with self.assertRaises(RevisionCorruptError):
                store.write_staged_revision(
                    tmp, STORAGE_KEY, digest_for(files), {'a.py': b'written'}, manifest_for(files)
                )
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual((destination / 'a.py').read_bytes(), b'promised')

    def test_write_staged_revision_sets_aside_a_destination_it_cannot_remove(self):
        # Removal needs to unlink the subtree, renaming needs only the parent directory, so a
        # tree that cannot be emptied can still be moved out of the way. That is what keeps one
        # bad write from poisoning a digest for every later retry.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'promised'}
            digest = digest_for(files)
            with (
                mock.patch.object(store.shutil, 'rmtree', refusing_to_remove(digest)),
                self.assertLogs(store.logger, 'ERROR') as logged,
                self.assertRaises(StorageError) as ctx,
            ):
                store.write_staged_revision(tmp, STORAGE_KEY, digest, {'a.py': b'written'}, manifest_for(files))

            message = str(ctx.exception)
            # Both halves of the story: what failed verification and what became of the tree.
            self.assertIn('does not match its manifest', message)
            self.assertIn('could not be removed', message)
            self.assertIn('set aside', message)
            self.assertTrue(any('set aside' in record for record in logged.output))

            revisions = pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions'
            self.assertFalse((revisions / digest).exists())
            quarantined = [path for path in revisions.iterdir() if store._QUARANTINE_SUFFIX in path.name]
            self.assertEqual(len(quarantined), 1)
            self.assertEqual((quarantined[0] / 'a.py').read_bytes(), b'written')
            self.assertIn(quarantined[0].name, message)

            # The point of setting it aside rather than leaving it: the retry now works.
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest, files, manifest_for(files))
            self.assertEqual((destination / 'a.py').read_bytes(), b'promised')

    def test_write_staged_revision_reports_a_destination_it_can_neither_remove_nor_set_aside(self):
        # Nothing can be done about the directory at this point, so the one thing that must not
        # happen is silence: the path is named so an operator and a later reconciler can find it.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'promised'}
            digest = digest_for(files)
            with (
                mock.patch.object(store.shutil, 'rmtree', refusing_to_remove(digest)),
                mock.patch.object(store.os, 'rename', refusing_to_quarantine()),
                self.assertLogs(store.logger, 'ERROR') as logged,
                self.assertRaises(StorageError) as ctx,
            ):
                store.write_staged_revision(tmp, STORAGE_KEY, digest, {'a.py': b'written'}, manifest_for(files))

            message = str(ctx.exception)
            self.assertIn('does not match its manifest', message)
            self.assertIn('could not be withdrawn', message)
            self.assertIn(digest, message)
            self.assertTrue(any('Could not withdraw' in record for record in logged.output))

            revisions = pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions'
            self.assertTrue((revisions / digest).exists())

    def test_write_staged_revision_does_not_remove_a_destination_another_writer_installed(self):
        # Losing the rename means the tree belongs to whoever won it. A corrupt one is
        # reported so an operator can look, never deleted out from under them.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'theirs'}
            destination = store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            (destination / 'a.py').write_bytes(b'tampered by someone else')
            with self.assertRaises(RevisionCorruptError):
                store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual((destination / 'a.py').read_bytes(), b'tampered by someone else')

    def test_write_staged_revision_rejects_a_digest_that_does_not_address_its_manifest(self):
        # Content addressing is the invariant: a revision directory is named by the digest of
        # the manifest it holds, so an incoherent pair is refused before anything is created.
        with tempfile.TemporaryDirectory() as tmp:
            files = {'a.py': b'aaa'}
            with self.assertRaises(RevisionCorruptError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, PLACEHOLDER_DIGEST, files, manifest_for(files))
            self.assertEqual(ctx.exception.reasons, ('digest_mismatch',))
            self.assertFalse((pathlib.Path(tmp) / str(STORAGE_KEY)).exists())

    def test_write_staged_revision_leaves_no_digest_directory_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.object(store, '_write_one_file', side_effect=OSError('no space left on device')),
                self.assertRaises(OSError),
            ):
                store.write_staged_revision(tmp, STORAGE_KEY, EMPTY_DIGEST, {'a.py': b'aaa'}, [])
            project = pathlib.Path(tmp) / str(STORAGE_KEY)
            self.assertFalse((project / 'revisions' / EMPTY_DIGEST).exists())
            self.assertEqual(list((project / 'staging').iterdir()), [])

    def test_write_staged_revision_rejects_a_traversal_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(UnsafePathError) as ctx:
                store.write_staged_revision(tmp, STORAGE_KEY, EMPTY_DIGEST, {'../escape.py': b'x'}, [])
            self.assertEqual(ctx.exception.code, 'path_traversal')
            self.assertFalse((pathlib.Path(tmp) / 'escape.py').exists())

    def test_write_staged_revision_rejects_a_path_escaping_the_revision_root(self):
        # normalize_source_path already rejects traversal, so the component gate is only
        # reachable with that front check bypassed.
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch('netbox_custom_scripts.storage.store.normalize_source_path', return_value='../escape.py'),
            self.assertRaises(UnsafePathError) as ctx,
        ):
            store.write_staged_revision(tmp, STORAGE_KEY, EMPTY_DIGEST, {'a.py': b'x'}, [])
        self.assertEqual(ctx.exception.code, 'escapes_root')
        self.assertFalse((pathlib.Path(tmp) / 'escape.py').exists())

    def test_write_staged_revision_refuses_a_symlinked_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            root = pathlib.Path(tmp) / 'root'
            root.mkdir()
            (root / str(STORAGE_KEY)).symlink_to(outside, target_is_directory=True)
            files = {'a.py': b'aaa'}
            with self.assertRaises(UnsafePathError) as ctx:
                store.write_staged_revision(root, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual(ctx.exception.code, 'symlink')
            self.assertEqual(list(outside.iterdir()), [])

    def test_write_staged_revision_works_under_a_symlinked_project_root(self):
        # The configured root is followed on purpose: pointing it at a symlinked volume is an
        # operator's choice, unlike a link appearing inside the tree.
        with tempfile.TemporaryDirectory() as tmp:
            real = pathlib.Path(tmp) / 'real_root'
            real.mkdir()
            link = pathlib.Path(tmp) / 'link_root'
            link.symlink_to(real, target_is_directory=True)
            files = {'a.py': b'aaa'}
            destination = store.write_staged_revision(link, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            self.assertEqual((destination / 'a.py').read_bytes(), b'aaa')
            store.verify_revision_tree(link, STORAGE_KEY, digest_for(files), manifest_for(files))
            store.delete_project_directory(link, STORAGE_KEY)
            self.assertFalse((real / str(STORAGE_KEY)).exists())

    def test_delete_revision_directory_removes_only_that_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            removed = {'a.py': b'a'}
            kept = {'b.py': b'b'}
            for files in (removed, kept):
                store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            store.delete_revision_directory(tmp, STORAGE_KEY, digest_for(removed))
            revisions = pathlib.Path(tmp) / str(STORAGE_KEY) / 'revisions'
            self.assertEqual([entry.name for entry in revisions.iterdir()], [digest_for(kept)])

    def test_delete_revision_directory_tolerates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.delete_revision_directory(tmp, STORAGE_KEY, PLACEHOLDER_DIGEST)

    def test_delete_revision_directory_refuses_a_symlinked_project_directory(self):
        # Following the link here would delete a tree that storage does not own.
        with tempfile.TemporaryDirectory() as tmp:
            outside = pathlib.Path(tmp) / 'outside'
            (outside / 'revisions' / PLACEHOLDER_DIGEST).mkdir(parents=True)
            (outside / 'revisions' / PLACEHOLDER_DIGEST / 'a.py').write_bytes(b'aaa')
            root = pathlib.Path(tmp) / 'root'
            root.mkdir()
            (root / str(STORAGE_KEY)).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(UnsafePathError) as ctx:
                store.delete_revision_directory(root, STORAGE_KEY, PLACEHOLDER_DIGEST)
            self.assertEqual(ctx.exception.code, 'symlink')
            self.assertTrue((outside / 'revisions' / PLACEHOLDER_DIGEST / 'a.py').exists())

    def test_delete_project_directory_removes_all_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            for files in ({'a.py': b'a'}, {'b.py': b'b'}):
                store.write_staged_revision(tmp, STORAGE_KEY, digest_for(files), files, manifest_for(files))
            store.delete_project_directory(tmp, STORAGE_KEY)
            self.assertFalse((pathlib.Path(tmp) / str(STORAGE_KEY)).exists())

    def test_delete_project_directory_tolerates_missing_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.delete_project_directory(tmp, STORAGE_KEY)

    def test_delete_project_directory_refuses_a_symlinked_project_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = pathlib.Path(tmp) / 'outside'
            outside.mkdir()
            (outside / 'keep.py').write_bytes(b'x')
            root = pathlib.Path(tmp) / 'root'
            root.mkdir()
            (root / str(STORAGE_KEY)).symlink_to(outside, target_is_directory=True)
            with self.assertRaises(OSError):
                store.delete_project_directory(root, STORAGE_KEY)
            self.assertTrue((outside / 'keep.py').exists())
