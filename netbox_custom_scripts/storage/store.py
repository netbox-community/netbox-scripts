"""
Storage-backend access to a project's stored revisions.

A revision's files are written as one key each, under the prefix that names the revision. No
backend offers a way to make a whole tree appear at once, so a revision being written is
visible under its prefix while it is incomplete, and the revision row's status is what tells a
reader whether the content is finished.

The manifest is the sole correctness boundary here. Every operation reads, verifies, and
removes exactly the keys the manifest names, so the backend contract stays the ordinary open,
save, exists, and delete calls, with no directory semantics. A key under the prefix that the
manifest does not name is inert: verification never reads it, and runtime materialization
copies manifest entries only, so it can never become an importable module. Reclaiming such
strays belongs to a future housekeeping reconciler, on backends that can enumerate them.

Content is trusted because it hashes to what the manifest recorded, not because of where it
appears to sit. That verification is also what makes writing safe to repeat: a key already
holding the recorded size and checksum is left alone and one holding anything else is
replaced, so an interrupted write is completed by the next attempt rather than blocking it.
The same read discipline is exported as copy_verified, so the runtime cache pulls bytes
under exactly the contract verification enforces and the two can never drift apart.
"""

import hashlib
import io
import logging
from pathlib import Path

from django.core.files.base import ContentFile

from .exceptions import RevisionCorruptError, StorageError, UnsafePathError
from .manifest import validate_manifest
from .paths import normalize_source_path, revision_key, revision_prefix

_HASH_CHUNK = 1024 * 1024

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.storage')


def write_revision(storage, storage_key, digest, files, manifest):
    """
    Write one revision's content and return the key prefix holding it.

    The manifest decides what is written, so a key the manifest does not describe is never
    created and the stored tree matches it by construction. The supplied mapping is
    canonicalized to look content up by the same path the manifest names.

    The whole tree is verified before this returns, so a caller that receives a prefix has
    content that matches its manifest. A tree that could not be written or verified is left
    in place rather than withdrawn: a concurrent writer of the same digest may already be
    counting on those exact keys, nothing serves content whose row never reported
    MATERIALIZED, the next attempt replaces whatever disagrees with the manifest, and the
    row's manifest still names every key for cleanup when the revision is deleted.

    Raises UnsafePathError when two source keys resolve to one canonical path (duplicate_path),
    RevisionCorruptError when the manifest or the stored tree does not hold up, and StorageError
    when the backend fails.
    """
    validate_manifest(manifest, digest)
    prefix = revision_prefix(storage_key, digest)
    source = _canonical_source(files)

    for entry in manifest:
        path = entry['path']
        if path not in source:
            raise StorageError(f'The manifest names "{path}", which the source mapping does not hold.')
        _write_one(storage, f'{prefix}{path}', source[path], entry)
    _verify_tree(storage, storage_key, digest, manifest)
    return prefix


def verify_revision_tree(storage, storage_key, digest, manifest):
    """
    Confirm a stored revision still matches its manifest, and return its key prefix.

    Every manifest entry is confirmed to be stored at the recorded size and to hash to the
    recorded checksum. Only manifest keys are read, so the backend needs no directory
    semantics and a stray key under the prefix stays inert. Raises RevisionCorruptError
    listing every mismatch, with the single reason prefix_missing when the revision is absent
    altogether, or StorageError when the backend cannot be read at all.

    The manifest is checked first. It arrives from a database row rather than from the builder,
    so treating its paths as safe would let the caller of a tampered row decide which key is
    read.
    """
    validate_manifest(manifest, digest)
    return _verify_tree(storage, storage_key, digest, manifest)


def _verify_tree(storage, storage_key, digest, manifest):
    """Walk one already validated manifest against the store, raising on any mismatch."""
    prefix = revision_prefix(storage_key, digest)
    expected = {entry['path']: entry for entry in manifest}

    reasons = []
    for path, entry in sorted(expected.items()):
        reasons.extend(_verify_entry(storage, f'{prefix}{path}', entry))

    if expected and len(reasons) == len(expected) and all(reason.startswith('missing:') for reason in reasons):
        # One reason rather than one per entry, because a revision that is entirely absent is a
        # different situation for an operator than a revision with damaged files in it.
        raise RevisionCorruptError(f'The stored revision at {prefix} is missing.', ['prefix_missing'])
    if reasons:
        raise RevisionCorruptError(f'The stored revision at {prefix} does not match its manifest.', reasons)
    return prefix


def delete_revision(storage, storage_key, digest, paths):
    """
    Remove one revision's stored files by their exact keys.

    The paths come from the revision's manifest, captured while its row still existed, so
    deletion never depends on the backend being able to list anything. A key that is already
    gone counts as removed, which is what makes retrying this operation safe. A path no key
    can be built from is collected like any other failure, so one bad payload entry cannot
    keep the remaining keys from being reclaimed. Failures are reported together in one
    StorageError, so a caller records one failure naming everything an operator or a future
    reconciler still has to reclaim.
    """
    failures = []
    for path in sorted(paths):
        try:
            key = revision_key(storage_key, digest, path)
            storage.delete(key)
        except FileNotFoundError:
            continue
        except Exception as error:
            failures.append(f'{path}: {error}')
    if failures:
        raise StorageError('Unable to remove stored revision content: ' + ', '.join(failures))


def copy_verified(storage, key, entry, destination=None):
    """
    Read one stored file, prove it matches its manifest entry, and optionally keep the bytes.

    The reported size is checked before the object is opened, because a remote backend may
    download the whole object on open, and the read itself never runs past one byte over the
    recorded size, so a lying stream costs bounded work. A matching size still gets its
    content read, because metadata cannot vouch for bytes. With a destination the bytes are
    written there while they are hashed, so a consumer only ever holds content that was
    measured. On failure a partial destination file may remain, for the caller to discard
    with its staging area.

    Raises RevisionCorruptError naming the manifest path when the content does not match,
    StorageError when the backend cannot be read, and the original OSError when the
    destination cannot accept the bytes.
    """
    if destination is None:
        _stream_verified(storage, key, entry, None)
        return
    with Path(destination).open('wb') as sink:  # cloud-compat: ok, the runtime cache's staging destination
        _stream_verified(storage, key, entry, _DestinationWriter(sink))


def read_verified(storage, key, entry):
    """
    Return one stored file's bytes, once they match their manifest entry.

    The in-memory counterpart of copy_verified, on the same bounded-read contract: the size is
    checked before the object is opened and the read stops one byte past the recorded size, so
    a lying stream costs bounded work and nothing over the manifest's size is ever held.

    Raises RevisionCorruptError naming the manifest path when the content does not match, and
    StorageError when the backend cannot be read.
    """
    sink = io.BytesIO()
    _stream_verified(storage, key, entry, sink)
    return sink.getvalue()


def read_revision_tree(storage, storage_key, digest, manifest):
    """
    Return one stored revision's whole tree, as a mapping of path to verified bytes.

    Staging a combined tree needs every existing file back in hand, which is why this exists
    alongside the streaming reads. The tree is therefore held in memory whole, bounded by the
    maximum project size the deployment configured.

    The manifest is validated before anything is read, for the same reason verify_revision_tree
    does it: the paths come from a database row and decide which keys are read.
    """
    validate_manifest(manifest, digest)
    prefix = revision_prefix(storage_key, digest)
    tree = {}
    for entry in manifest:
        path = entry['path']
        tree[path] = read_verified(storage, f'{prefix}{path}', entry)
    return tree


def _canonical_source(files):
    """
    Return the supplied mapping keyed by canonical path.

    Two source keys that resolve to one canonical path would silently store one and drop the
    other, so the collision is refused here rather than resolved by mapping order.
    """
    source = {}
    for path, content in files.items():
        canonical = normalize_source_path(path)
        if canonical in source:
            raise UnsafePathError(path, 'duplicate_path', f'Multiple source files resolve to the path "{canonical}".')
        source[canonical] = content
    return source


def _write_one(storage, key, content, entry):
    """
    Store one file, leaving a key that already holds the recorded content alone.

    Storage.save() invents a new name when a key is occupied, which in a content-addressed
    store means a file written where nothing will ever look for it. An occupied key is
    therefore resolved before save() is reached: one already holding the recorded content is
    left alone, and one holding anything else is removed first, because the digest has already
    fixed what the content must be.
    """
    if _exists(storage, key):
        if not _verify_entry(storage, key, entry):
            return
        _delete(storage, key)

    try:
        stored = storage.save(key, ContentFile(content))
    except Exception as error:
        raise StorageError(f'Unable to store "{key}": {error}') from error
    if stored != key:
        # The key was taken in the window between the check above and the save, so the backend
        # parked this write under an invented name. Remove that stray object, then look at
        # what occupies the key: a competing writer of the same digest writes byte-identical
        # content, and finding it there means the revision this call was asked to produce
        # exists. Anything else at the key is a real failure.
        try:
            storage.delete(stored)
        except Exception as error:
            # The stray sits in no manifest, so nothing can rediscover it later. Naming it
            # here is its one record until the housekeeping reconciler owns orphans.
            logger.error('Could not remove the stray object "%s" left by a renamed save: %s', stored, error)
        if _verify_entry(storage, key, entry):
            raise StorageError(f'The storage backend stored "{key}" as "{stored}" instead of the key it was given.')


def _verify_entry(storage, key, entry):
    """Return the mismatch reasons for one manifest entry, or an empty list when it is intact."""
    try:
        copy_verified(storage, key, entry)
    except RevisionCorruptError as error:
        return list(error.reasons)
    return []


def _stream_verified(storage, key, entry, sink):
    """Verify one stored object against its manifest entry, streaming the bytes to sink when given."""
    path = entry['path']
    mismatch = f'The stored file "{key}" does not match its manifest entry.'
    try:
        reported = _size(storage, key)
        if reported is not None and reported != entry['size']:
            raise RevisionCorruptError(mismatch, [f'size_mismatch:{path}'])
        with storage.open(key, 'rb') as handle:
            size, checksum = _measure(handle, entry['size'], sink)
    except FileNotFoundError as error:
        raise RevisionCorruptError(mismatch, [f'missing:{path}']) from error
    except _DestinationError as error:
        raise error.original from None
    except (RevisionCorruptError, StorageError):
        raise
    except Exception as error:
        raise StorageError(f'Unable to read the stored file "{key}": {error}') from error

    if size != entry['size']:
        raise RevisionCorruptError(mismatch, [f'size_mismatch:{path}'])
    if checksum != entry['sha256']:
        raise RevisionCorruptError(mismatch, [f'checksum_mismatch:{path}'])


class _DestinationError(Exception):
    """Carries a destination write failure across the backend failure classification."""

    def __init__(self, original):
        super().__init__(str(original))
        self.original = original


class _DestinationWriter:
    """Tags destination write failures, so they never classify as backend read failures."""

    def __init__(self, sink):
        self._sink = sink

    def write(self, chunk):
        try:
            self._sink.write(chunk)
        except OSError as error:
            raise _DestinationError(error) from error


def _measure(handle, expected_size, sink=None):
    """
    Return the (size, sha256) of an open file, reading at most one byte past expected_size.

    The manifest already fixed the only acceptable size, so reading further buys nothing.
    This bound is the layer that holds when a backend reports no size or one its stream does
    not honor. A longer stream reports as expected_size + 1, which the caller rejects on
    size alone. The read stays chunked, so nothing is ever held in memory whole, and each
    chunk is handed to the sink as it is measured when one is given.
    """
    size = 0
    digest = hashlib.sha256()
    remaining = expected_size + 1
    while remaining > 0:
        chunk = handle.read(min(_HASH_CHUNK, remaining))
        if not chunk:
            break
        size += len(chunk)
        remaining -= len(chunk)
        digest.update(chunk)
        if sink is not None:
            sink.write(chunk)
    return size, digest.hexdigest()


def _exists(storage, key):
    """Report whether a key is occupied, as a StorageError when the backend cannot say."""
    try:
        return storage.exists(key)
    except Exception as error:
        raise StorageError(f'Unable to inspect whether "{key}" exists: {error}') from error


def _size(storage, key):
    """
    Return the backend's reported size for a key, or None when it cannot say.

    FileNotFoundError propagates so absence classifies the same from a metadata probe and
    a read.
    """
    try:
        return storage.size(key)
    except (NotImplementedError, AttributeError):
        return None
    except FileNotFoundError:
        raise
    except Exception as error:
        raise StorageError(f'Unable to inspect the size of "{key}": {error}') from error


def _delete(storage, key):
    """Remove one object, reporting a backend failure as a StorageError naming the key."""
    try:
        storage.delete(key)
    except Exception as error:
        raise StorageError(f'Unable to remove "{key}": {error}') from error
