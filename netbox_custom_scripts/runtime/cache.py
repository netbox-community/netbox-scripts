"""
Disposable on-disk materialization of stored revisions.

Python imports need a real directory tree, and this cache is the one sanctioned local write
tier that provides it. The authoritative bytes stay behind the Django storage backend, so a
cached tree is only ever a regenerated copy: every use re-verifies it against the revision
manifest, compiled files are purged before any verification because a planted one can be
flagged to skip its own source check, and a tree that fails is set aside and rebuilt through
the store's bounded verified reads. Losing the cache costs a rebuild, never data.

A published tree is immutable. It appears with one rename, is write-protected before
materialization returns it, and its content is never repaired in place, so a reader that
resolved the directory can trust what verification just proved about it. Concurrent builders
of one revision serialize on a per-slot file lock that the operating system releases with the
owning process, and eviction is deliberately absent, reclaiming stale slots belongs to a
housekeeping reconciler.
"""

import fcntl
import hashlib
import logging
import os
import shutil
import stat
import tempfile  # cloud-compat: ok, the default cache root is per-pod scratch by design
import uuid
from contextlib import contextmanager
from pathlib import Path

from netbox.plugins import get_plugin_config

from ..storage.manifest import validate_manifest
from ..storage.paths import _digest_component, _storage_key_component, revision_key
from ..storage.store import copy_verified
from .exceptions import LocalCacheCorruptError, LocalCacheError

__all__ = (
    'local_revision_dir',
    'materialize_revision',
    'resolve_cache_root',
    'verify_local_tree',
)

_PLUGIN_NAME = 'netbox_custom_scripts'
_HASH_CHUNK = 1024 * 1024
_MAX_MATERIALIZE_ATTEMPTS = 3
_BYTECODE_SUFFIXES = ('.pyc', '.pyo')

# Linux bounds a path at 4096 bytes including the terminator. Staying comfortably under it
# keeps every name composed beside the slot usable too, staging and corrupt-aside siblings
# and the interpreter's compiled-file names, rather than failing on whichever loses the
# remaining headroom first.
_MAX_LOCAL_PATH_BYTES = 3800

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.runtime')


def resolve_cache_root():
    """
    Return the directory the runtime cache lives under.

    The plugin setting is read on every call, so a test override is honored without any
    module state to reset. The default sits under the system temporary directory, which is
    per-pod, writable, and disposable on the hosted platforms, exactly the guarantees the
    cache is designed around.
    """
    configured = get_plugin_config(_PLUGIN_NAME, 'runtime_cache_root')
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / 'netbox-custom-scripts' / 'runtime-cache'


def local_revision_dir(storage_key, digest, cache_root=None):
    """
    Return the directory one revision's tree occupies, without touching the filesystem.

    Both components pass the guards backend keys pass, so a value that cannot name a stored
    location cannot name a cache location either, and the two layouts stay in lockstep.
    """
    root = Path(cache_root) if cache_root is not None else resolve_cache_root()
    return root / _storage_key_component(storage_key) / _digest_component(digest)


def verify_local_tree(local_dir, manifest):
    """
    Prove one local tree holds exactly its manifest's files, and nothing else.

    Every entry must be a regular file carrying the recorded size and checksum. Inspection
    uses lstat throughout, so a symlink anywhere fails the tree instead of being followed,
    and anything the manifest does not name fails it too, including a directory no entry
    sits under. There is no allowance for compiled files, verification runs after the purge
    and judges what is actually on disk. Raises LocalCacheCorruptError listing every reason,
    with the single reason missing_root when there is no directory to verify at all, and
    LocalCacheError when the tree cannot be read. The caller owns manifest validation, the
    tree is judged against the manifest exactly as given.
    """
    local_dir = Path(local_dir)
    failure = f'The cached tree at "{local_dir}" does not match the revision manifest.'
    root = _lstat_or_none(local_dir)
    if root is None or stat.S_ISLNK(root.st_mode) or not stat.S_ISDIR(root.st_mode):
        raise LocalCacheCorruptError(failure, ['missing_root'])

    expected = {entry['path']: entry for entry in manifest}
    expected_dirs = set()
    for path in expected:
        segments = path.split('/')
        expected_dirs.update('/'.join(segments[:index]) for index in range(1, len(segments)))

    try:
        entries = list(_scan_tree(local_dir))
    except OSError as error:
        raise LocalCacheError(f'Unable to inspect the cached tree "{local_dir}": {error}') from error

    reasons = []
    flagged = set()
    found = {}
    for relative, info in entries:
        if stat.S_ISLNK(info.st_mode):
            reasons.append(f'symlink:{relative}')
            flagged.add(relative)
        elif stat.S_ISDIR(info.st_mode):
            if relative not in expected_dirs:
                reasons.append(f'unexpected_entry:{relative}')
                flagged.add(relative)
        elif not stat.S_ISREG(info.st_mode):
            reasons.append(f'unexpected_entry:{relative}')
            flagged.add(relative)
        elif relative in expected:
            found[relative] = info
        else:
            reasons.append(f'unexpected_entry:{relative}')
            flagged.add(relative)

    for path, entry in expected.items():
        info = found.get(path)
        if info is None:
            if path not in flagged:
                reasons.append(f'missing:{path}')
            continue
        if info.st_size != entry['size']:
            reasons.append(f'size_mismatch:{path}')
            continue
        size, checksum = _measure_file(local_dir / path, entry['size'])
        if size != entry['size']:
            reasons.append(f'size_mismatch:{path}')
        elif checksum != entry['sha256']:
            reasons.append(f'checksum_mismatch:{path}')

    if reasons:
        raise LocalCacheCorruptError(failure, sorted(reasons))


def materialize_revision(storage, storage_key, digest, manifest, cache_root=None):
    """
    Return a local directory that verifiably holds one revision's tree, building it if needed.

    The manifest is validated first, including its digest binding, so a tampered row never
    steers a filesystem path or a backend key. The slot is settled under an exclusive
    cross-process lock: compiled files are purged, a tree already present is re-verified and
    returned without any backend traffic, a failed tree is set aside beside the slot, and a
    fresh tree is staged as a sibling through bounded verified reads, verified as a whole,
    published with one rename, and write-protected before it is returned. Only this process's
    own staging verification failures are retried, a bounded number of times.

    Raises RevisionCorruptError when the manifest or the authoritative content cannot be
    trusted, StorageError when the backend fails, and LocalCacheError when the local side
    cannot deliver, including the retry bound and a cache root too deep for this revision's
    paths.
    """
    validate_manifest(manifest, digest)
    target = local_revision_dir(storage_key, digest, cache_root)
    project_dir = target.parent
    _guard_path_budget(project_dir, digest, manifest)
    _ensure_private_root(project_dir.parent)
    try:
        project_dir.mkdir(parents=True, exist_ok=True)  # cloud-compat: ok, the runtime cache tier
    except OSError as error:
        raise LocalCacheError(f'Unable to create the cache directory "{project_dir}": {error}') from error

    last_failure = None
    with _slot_lock(project_dir / f'.{digest}.lock'):
        for _attempt in range(_MAX_MATERIALIZE_ATTEMPTS):
            if _existing_tree_verifies(target, manifest):
                # A build interrupted between its rename and its write-protect leaves a tree
                # that verifies and can still take bytecode.
                _make_read_only(target)
                return target
            staging = project_dir / f'.{digest}.staging.{uuid.uuid4().hex}'
            try:
                _fill_staging(storage, storage_key, digest, manifest, staging)
                try:
                    verify_local_tree(staging, manifest)
                except LocalCacheCorruptError as error:
                    last_failure = error
                    logger.warning('The staged tree for revision %s failed verification (%s), retrying', digest, error)
                    continue
                _publish(staging, target, manifest)
                # A host may check write permission on the directory being renamed rather
                # than only on its parent.
                _make_read_only(target)
                return target
            finally:
                if _lstat_or_none(staging) is not None:
                    shutil.rmtree(staging, onexc=_log_staging_residue)  # cloud-compat: ok, our own staging tree

    reasons = ', '.join(last_failure.reasons) if last_failure else 'unknown'
    raise LocalCacheError(
        f'The staged tree for "{target}" failed verification {_MAX_MATERIALIZE_ATTEMPTS} times in a row, '
        f'giving up on this attempt. Last reasons: {reasons}.'
    )


def _ensure_private_root(root):
    """
    Create the cache root private to this user, refusing a root anyone else could substitute.

    The loader imports from a verified tree, so an ancestor another user can rename is a
    substitution window that verification cannot close. A group or world writable ancestor is
    accepted only when it is sticky, which is what keeps a shared temporary directory usable.
    """
    # Every level this function creates is created private. mkdir(parents=True) applies its mode
    # to the leaf only, so an intermediate directory left at the process umask would be group
    # writable, and the check below would then reject a root this code had just made itself.
    missing = [directory for directory in (root, *root.parents) if not directory.exists()]
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)  # cloud-compat: ok, the runtime cache tier
        except FileExistsError:
            continue
        except OSError as error:
            raise LocalCacheError(f'Unable to create the cache root "{directory}": {error}') from error
    for directory in (root, *root.parents):
        try:
            info = directory.stat()
        except OSError as error:
            raise LocalCacheError(f'Unable to inspect the cache root "{directory}": {error}') from error
        if info.st_uid not in (os.getuid(), 0):
            raise LocalCacheError(
                f'The cache path "{directory}" belongs to another user, who could substitute the '
                f'tree the loader imports. Point runtime_cache_root somewhere this process owns.'
            )
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH) and not info.st_mode & stat.S_ISVTX:
            raise LocalCacheError(
                f'The cache path "{directory}" is writable by other users and not sticky, so the '
                f'tree the loader imports could be substituted. Restrict it with '
                f'"chmod 700 {directory}", or point runtime_cache_root somewhere already private.'
            )


def _guard_path_budget(project_dir, digest, manifest):
    """Refuse a cache root that leaves no room for this revision's longest composed path."""
    staging_name = f'.{digest}.staging.{"0" * 32}'
    longest_entry = max((len(entry['path'].encode('utf-8')) for entry in manifest), default=0)
    longest = len(str(project_dir / staging_name).encode('utf-8')) + 1 + longest_entry
    if longest > _MAX_LOCAL_PATH_BYTES:
        raise LocalCacheError(
            f'The cache root leaves this revision a longest path of {longest} bytes, over the '
            f'{_MAX_LOCAL_PATH_BYTES} byte budget. Point runtime_cache_root at a shorter location.'
        )


def _log_staging_residue(_function, path, error):
    """Report a staging entry that could not be removed."""
    # This runs from a finally block, so raising would replace the failure actually being
    # reported with a cleanup failure.
    logger.warning('Unable to remove "%s" from the staging tree: %s', path, error)


def _reraise(error):
    """Surface a walk error instead of silently skipping the subtree it belongs to."""
    raise error


@contextmanager
def _slot_lock(lock_path):
    """
    Hold the cross-process lock for one revision's cache slot.

    flock ties the lock to the open descriptor, so a process killed mid-build releases it
    with its file table and never wedges the slot. The lock file itself is inert and stays
    behind, it sits beside the slot rather than inside it, where no verification ever looks.
    """
    try:
        handle = lock_path.open('ab')  # cloud-compat: ok, the slot lock lives beside the cache it serializes
    except OSError as error:
        raise LocalCacheError(f'Unable to open the cache lock "{lock_path}": {error}') from error
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise LocalCacheError(f'Unable to take the cache lock "{lock_path}": {error}') from error
        yield
    finally:
        handle.close()


def _existing_tree_verifies(target, manifest):
    """Judge the tree already in the slot, setting a failed one aside. Returns True on a hit."""
    _purge_bytecode(target)
    if _lstat_or_none(target) is None:
        return False
    try:
        verify_local_tree(target, manifest)
    except LocalCacheCorruptError as error:
        _set_aside_corrupt(target, error)
        return False
    return True


def _set_aside_corrupt(target, error):
    """
    Move a failed tree out of the slot without destroying it.

    The tree is evidence of what went wrong, so it is renamed beside the slot rather than
    removed in place, and the housekeeping reconciler owns reclaiming it. Raises
    LocalCacheError when it cannot be moved.
    """
    aside = target.with_name(f'{target.name}.corrupt.{uuid.uuid4().hex}')
    logger.warning(
        'The cached tree "%s" failed verification (%s), setting it aside as "%s"',
        target,
        ', '.join(error.reasons),
        aside.name,
    )
    info = _lstat_or_none(target)
    # A published tree lends its own write bit for the rename, under the same host rule
    # materialize_revision notes. Directories only, chmod follows a symlink.
    lend = info is not None and stat.S_ISDIR(info.st_mode) and not stat.S_IMODE(info.st_mode) & stat.S_IWUSR
    try:
        if lend:
            target.chmod(stat.S_IMODE(info.st_mode) | stat.S_IWUSR)  # cloud-compat: ok, the runtime cache tier
        target.rename(aside)  # cloud-compat: ok, the runtime cache tier
    except OSError as rename_error:
        raise LocalCacheError(f'Unable to set the failed tree "{target}" aside: {rename_error}') from rename_error


def _purge_bytecode(local_dir):
    """
    Remove every compiled Python artifact under one tree.

    A compiled file can be flagged to skip its own source hash check, so a planted one would
    execute unverified no matter what the manifest proves about the sources. Removing them
    before verification is what closes that door, the read-only publish only stops the
    interpreter from recreating them. Raises LocalCacheError when an artifact cannot be
    removed, because a tree that cannot be cleared cannot be trusted either.
    """
    info = _lstat_or_none(local_dir)
    if info is None or not stat.S_ISDIR(info.st_mode):
        # Nothing here can execute, and verification names what actually occupies the slot.
        return
    try:
        for base, directories, files in local_dir.walk(top_down=False, on_error=_reraise):
            for name in files:
                if name.lower().endswith(_BYTECODE_SUFFIXES):
                    (base / name).unlink()  # cloud-compat: ok, compiled artifacts in the disposable cache
            for name in directories:
                if name.lower() != '__pycache__':
                    continue
                candidate = base / name
                if candidate.is_symlink():
                    candidate.unlink()  # cloud-compat: ok, compiled artifacts in the disposable cache
                else:
                    shutil.rmtree(candidate)  # cloud-compat: ok, compiled artifacts in the disposable cache
    except OSError as error:
        raise LocalCacheError(f'Unable to remove compiled artifacts under "{local_dir}": {error}') from error


def _fill_staging(storage, storage_key, digest, manifest, staging):
    """Pull every manifest entry into the staging tree through the store's bounded verified read."""
    try:
        staging.mkdir()  # cloud-compat: ok, our own staging tree
    except OSError as error:
        raise LocalCacheError(f'Unable to create the staging tree "{staging}": {error}') from error
    for entry in manifest:
        destination = staging / entry['path']
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)  # cloud-compat: ok, our own staging tree
        except OSError as error:
            raise LocalCacheError(f'Unable to create a directory for "{entry["path"]}": {error}') from error
        try:
            copy_verified(storage, revision_key(storage_key, digest, entry['path']), entry, destination)
        except OSError as error:
            # copy_verified reports its backend and content failures itself, an OSError out
            # of it is this host failing to accept the bytes.
            raise LocalCacheError(f'Unable to write "{entry["path"]}" into the staging tree: {error}') from error


def _make_read_only(root):
    """Drop every write bit, so the interpreter never writes compiled files into a published tree."""
    try:
        for base, directories, files in root.walk(top_down=False, on_error=_reraise):
            for name in files:
                (base / name).chmod(0o444)  # cloud-compat: ok, the runtime cache tier
            for name in directories:
                (base / name).chmod(0o555)  # cloud-compat: ok, the runtime cache tier
        root.chmod(0o555)  # cloud-compat: ok, the runtime cache tier
    except OSError as error:
        raise LocalCacheError(f'Unable to write-protect the published tree "{root}": {error}') from error


def _publish(staging, target, manifest):
    """Move the verified staging tree into the slot with one rename."""
    try:
        staging.rename(target)  # cloud-compat: ok, publishing into the runtime cache
    except OSError as error:
        # The slot was taken between this builder's own check and its rename. Whoever owns
        # it now published a verified tree or left damage, judging the occupant settles
        # which, and a builder that lost the race to a good tree has still succeeded.
        try:
            verify_local_tree(target, manifest)
        except LocalCacheCorruptError:
            raise LocalCacheError(f'Unable to publish the staged tree into "{target}": {error}') from error


def _scan_tree(local_dir):
    """Yield (relative path, lstat result) for every entry under one root, following nothing."""
    for base, directories, files in local_dir.walk(on_error=_reraise):
        for name in (*directories, *files):
            node = base / name
            yield node.relative_to(local_dir).as_posix(), node.lstat()


def _measure_file(path, expected_size):
    """Return the (size, sha256) of one local file, reading at most one byte past expected_size."""
    size = 0
    digest = hashlib.sha256()
    remaining = expected_size + 1
    try:
        with path.open('rb') as handle:
            while remaining > 0:
                chunk = handle.read(min(_HASH_CHUNK, remaining))
                if not chunk:
                    break
                size += len(chunk)
                remaining -= len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise LocalCacheError(f'Unable to read "{path}" for verification: {error}') from error
    return size, digest.hexdigest()


def _lstat_or_none(path):
    """Return the lstat result for a path, or None when nothing occupies it."""
    try:
        return path.lstat()
    except FileNotFoundError:
        return None
