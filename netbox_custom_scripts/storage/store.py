"""
Filesystem access to a project's source tree and its stored revisions.

A source tree is walked in a stable order, enforcing the limits it is handed as the tree is
read, so an oversized tree is rejected without being loaded into memory. Revision
directories are written through a staging directory and renamed into place, so a partial
tree is never visible.

Every component below the configured project root is opened one at a time through a
directory descriptor that refuses a symbolic link, so a link planted anywhere along the way
cannot redirect a read, a write, or a removal outside the root. The configured root itself
is followed, because an operator may point it at a symlinked volume. Source trees are walked
the same way, and there the root is refused if it is a link, since a source directory is
supplied per operation rather than configured once by an operator.
"""

import contextlib
import errno
import hashlib
import logging
import os
import shutil
import stat
import uuid
from pathlib import Path

from .exceptions import LimitExceededError, RevisionCorruptError, StorageError, UnsafePathError
from .manifest import validate_manifest
from .paths import (
    MAX_PATH_DEPTH,
    normalize_source_path,
    project_directory,
    revision_directory,
    staging_directory,
)

_HASH_CHUNK = 1024 * 1024

# What a revision directory is renamed to when it cannot be verified and cannot be removed. A
# digest is 64 hexadecimal characters, so a name carrying this can never collide with one.
_QUARANTINE_SUFFIX = '.corrupt.'

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.storage')


def _entry_kind(child, relative, noun):
    """
    Classify one directory entry as a symlink, a directory, a regular file, or something else.

    os.scandir usually answers this from the directory listing it already read, but it falls
    back to a stat call, which fails when the entry disappears or its permissions change between
    the listing and the question. That failure belongs on the same boundary as opening and
    reading the file, so it is reported as a StorageError rather than escaping as a bare OSError.
    """
    try:
        if child.is_symlink():
            return 'symlink'
        if child.is_dir(follow_symlinks=False):
            return 'directory'
        return 'file' if child.is_file(follow_symlinks=False) else 'special'
    except OSError as error:
        raise StorageError(f'Unable to inspect the {noun} entry "{relative}": {error}') from error


def _read_regular_file(name, relative, max_file_size, dir_fd):
    """
    Return the content of one directory entry, read once through a validated descriptor.

    The entry is opened by name against its parent's descriptor, so no part of the path is
    re-resolved and a directory swapped for a link after the walk saw it cannot redirect the
    read. The opened descriptor is confirmed to be a regular file, and at most
    max_file_size + 1 bytes are read so a growing or replaced file cannot exceed the supplied
    memory bound. Raises UnsafePathError (special_file) for a non-regular file,
    LimitExceededError (file_too_large) when the content is over the limit, and StorageError
    for any operating-system read failure.
    """
    try:
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(descriptor, 'rb') as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnsafePathError(relative, 'special_file', 'Special files are not allowed in a source tree.')
            content = handle.read(max_file_size + 1)
    except OSError as error:
        raise StorageError(f'Unable to read source file "{relative}": {error}') from error
    if len(content) > max_file_size:
        raise LimitExceededError(
            'file_too_large', f'"{relative}" is over the {max_file_size} byte limit.', path=relative
        )
    return content


def iter_directory_files(root, limits):
    """
    Yield (raw_relative_posix_path, content_bytes) for every regular file under root.

    The tree is walked top down and in sorted order within each directory, entirely through
    directory descriptors: each level is listed with os.scandir on an open descriptor, each
    subdirectory is opened with O_NOFOLLOW against its parent, and each file is opened by
    name against the descriptor of the directory it was listed in. Nothing is re-resolved
    from a pathname, so a directory that is swapped for a symbolic link after the walk has
    listed it cannot redirect a later read outside the root. That matters because this is a
    generator: the window between listing a directory and reading its files is as long as
    the consumer takes.

    Unlike the configured storage root, which may legitimately be a symlinked volume, the
    source root itself is opened with O_NOFOLLOW, since a caller pointing this at a link is
    the case the walker exists to refuse.

    Paths are yielded unmodified so the manifest builder owns normalization and duplicate
    detection. The supplied per-file, file-count, and total-size limits are enforced as the
    tree is read, and each file is opened once and read with a bound, so an oversized tree is
    rejected without being loaded into memory. Raises UnsafePathError on a symbolic link
    (symlink), a special file (special_file), or a tree deeper than the path policy allows
    (path_too_deep), StorageError when the root is missing, is not a directory, cannot be read,
    or holds an entry that cannot be inspected, and LimitExceededError when a limit is exceeded.
    """
    root = Path(root)
    if not root.exists():
        raise StorageError(f'The source tree root does not exist: {root}')
    if not root.is_dir():
        raise StorageError(f'The source tree root is not a directory: {root}')

    count = 0
    total = 0

    def walk(dir_fd, prefix, depth):
        nonlocal count, total
        try:
            with os.scandir(dir_fd) as entries:
                children = sorted(entries, key=lambda child: child.name)
        except OSError as error:
            raise StorageError(f'Unable to read the source tree: {error}') from error

        subdirectories = []
        for child in children:
            relative = f'{prefix}{child.name}'
            kind = _entry_kind(child, relative, 'source')
            if kind == 'symlink':
                raise UnsafePathError(relative, 'symlink', 'Symbolic links are not allowed in a source tree.')
            if kind == 'directory':
                subdirectories.append(child.name)
                continue
            # A special file is not separated out here. _read_regular_file confirms the opened
            # descriptor with fstat, which is the answer that cannot be raced.
            count += 1
            if count > limits.max_file_count:
                raise LimitExceededError(
                    'too_many_files', f'The source tree has more than {limits.max_file_count} files.'
                )
            content = _read_regular_file(child.name, relative, limits.max_file_size, dir_fd)
            total += len(content)
            if total > limits.max_project_size:
                raise LimitExceededError(
                    'project_too_large', f'The source tree exceeds the {limits.max_project_size} byte limit.'
                )
            yield relative, content

        # Files before subdirectories, matching the top-down order the walk used before, so
        # a caller that depended on it sees no change.
        for name in subdirectories:
            if depth + 1 > MAX_PATH_DEPTH:
                raise UnsafePathError(
                    f'{prefix}{name}',
                    'path_too_deep',
                    f'The source tree is deeper than the {MAX_PATH_DEPTH} level limit.',
                )
            descriptor = _open_child(name, dir_fd)
            try:
                yield from walk(descriptor, f'{prefix}{name}/', depth + 1)
            finally:
                os.close(descriptor)

    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(str(root), 'symlink', 'The source tree root must not be a symbolic link.') from error
        raise StorageError(f'Unable to open the source tree root "{root}": {error}') from error
    try:
        yield from walk(root_fd, '', 0)
    finally:
        os.close(root_fd)


def _storage_components(project_root, path):
    """Return the components of one storage path, relative to the configured project root."""
    return path.relative_to(project_root).parts


def _open_root(stack, project_root):
    """
    Open the configured project root and register its descriptor for closing.

    The root is followed, unlike every component beneath it, because pointing the setting at
    a symlinked volume is a normal operator choice rather than a tampering signal.
    """
    try:
        descriptor = os.open(project_root, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as error:
        raise StorageError(f'Unable to open the project storage root "{project_root}": {error}') from error
    stack.callback(os.close, descriptor)
    return descriptor


def _open_child(name, dir_fd, create=False):
    """
    Return a descriptor for one child directory, refusing to follow a symbolic link.

    Raises UnsafePathError (symlink) when the component is a link, UnsafePathError
    (not_a_directory) when it exists as something else, and lets FileNotFoundError through so
    a caller can tell a missing tree from a tampered one.
    """
    if create:
        try:
            os.mkdir(name, dir_fd=dir_fd)
        except FileExistsError:
            pass
        except OSError as error:
            raise StorageError(f'Unable to create the storage directory "{name}": {error}') from error
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafePathError(name, *_refusal(name, dir_fd)) from error
        raise StorageError(f'Unable to open the storage directory "{name}": {error}') from error


def _refusal(name, dir_fd):
    """
    Return the (code, message) describing why a component could not be opened as a directory.

    O_DIRECTORY and O_NOFOLLOW both reject a symbolic link, and which errno arrives depends on
    the kernel, so the component is inspected rather than inferred from the errno. This runs
    only after a refusal and decides the label alone, never whether to proceed.
    """
    try:
        if stat.S_ISLNK(os.lstat(name, dir_fd=dir_fd).st_mode):
            return 'symlink', f'"{name}" is a symbolic link.'
    except OSError:
        pass
    return 'not_a_directory', f'"{name}" is not a directory.'


def _open_child_in(stack, name, dir_fd, create=False):
    """Open one child directory and register its descriptor for closing."""
    descriptor = _open_child(name, dir_fd, create=create)
    stack.callback(os.close, descriptor)
    return descriptor


def _directory_exists(dir_fd, name):
    """Return whether one child exists as a real directory, following no symbolic link."""
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode)


def verify_revision_tree(project_root, storage_key, digest, manifest):
    """
    Check that a stored revision directory still matches its manifest.

    Existence of the directory is not evidence that its contents are intact, so every
    manifest entry is confirmed to be a regular file of the recorded size and checksum, and
    the directory is confirmed to hold nothing else. Directories count as content: an
    unexpected one can become an importable namespace package, and a symbolic link can expose
    modules the manifest never recorded. Raises RevisionCorruptError listing every mismatch,
    or StorageError when the tree cannot be read at all.

    The manifest is itself checked first. It arrives from a database row rather than from the
    builder, so treating its paths as safe would let the caller of a tampered row decide which
    file gets read.
    """
    validate_manifest(manifest, digest)
    root = revision_directory(project_root, storage_key, digest)
    expected = {entry['path']: entry for entry in manifest}

    with contextlib.ExitStack() as stack:
        root_fd = _open_root(stack, project_root)
        try:
            revision_fd = root_fd
            for component in _storage_components(project_root, root):
                revision_fd = _open_child_in(stack, component, revision_fd)
        except UnsafePathError as error:
            reason = 'root_symlink' if error.code == 'symlink' else 'root_not_directory'
            raise RevisionCorruptError(
                f'The revision directory at {root} is not safely reachable: {error}', [reason]
            ) from error
        except FileNotFoundError as error:
            raise RevisionCorruptError(f'The revision directory is missing: {root}', ['root_missing']) from error

        reasons = []
        for path, entry in sorted(expected.items()):
            reasons.extend(_verify_entry(revision_fd, path, entry))
        reasons.extend(_report_unexpected(revision_fd, expected))

    if reasons:
        raise RevisionCorruptError(f'The stored revision at {root} does not match its manifest.', reasons)
    return root


def _verify_entry(revision_fd, path, entry):
    """Return the mismatch reasons for one manifest entry, refusing every symbolic link."""
    *directories, name = path.split('/')
    with contextlib.ExitStack() as stack:
        try:
            parent_fd = revision_fd
            for component in directories:
                parent_fd = _open_child_in(stack, component, parent_fd)
        except UnsafePathError:
            return [f'symlink:{path}']
        except FileNotFoundError:
            return [f'missing_or_special:{path}']

        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                return [f'symlink:{path}']
            if error.errno in (errno.ENOENT, errno.ENOTDIR):
                return [f'missing_or_special:{path}']
            raise StorageError(f'Unable to read the stored file "{path}": {error}') from error

        with os.fdopen(descriptor, 'rb') as handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode):
                return [f'missing_or_special:{path}']
            if info.st_size != entry['size']:
                return [f'size_mismatch:{path}']
            if _hash_handle(handle, path) != entry['sha256']:
                return [f'checksum_mismatch:{path}']
    return []


def _report_unexpected(revision_fd, expected):
    """Return a reason for everything the tree holds that its manifest does not describe."""
    expected_directories = set()
    for path in expected:
        components = path.split('/')
        expected_directories.update('/'.join(components[:index]) for index in range(1, len(components)))

    files, directories, symlinks, specials, too_deep = _found_entries(revision_fd)
    return [
        *(f'unexpected_file:{path}' for path in sorted(files - set(expected))),
        *(f'unexpected_directory:{path}' for path in sorted(directories - expected_directories)),
        *(f'unexpected_symlink:{path}' for path in sorted(symlinks)),
        *(f'unexpected_special:{path}' for path in sorted(specials)),
        *(f'unexpected_depth:{path}' for path in sorted(too_deep)),
    ]


def _found_entries(revision_fd):
    """
    Return the (files, directories, symlinks, specials, too_deep) a stored revision holds.

    The walk is iterative and depth-bounded on purpose. It reads whatever is on disk rather
    than what the manifest promised, so the source-path policy cannot protect it: a tree
    planted deeper than the limit would otherwise exhaust the Python stack here, after the
    rename, which is the worst place to fail.
    """
    files = set()
    directories = set()
    symlinks = set()
    specials = set()
    too_deep = set()

    def walk(dir_fd, prefix, depth):
        try:
            with os.scandir(dir_fd) as entries:
                children = sorted(entries, key=lambda child: child.name)
        except OSError as error:
            raise StorageError(f'Unable to read the stored revision: {error}') from error
        for child in children:
            relative = f'{prefix}{child.name}'
            kind = _entry_kind(child, relative, 'stored')
            if kind == 'symlink':
                symlinks.add(relative)
            elif kind == 'directory':
                directories.add(relative)
                if depth + 1 > MAX_PATH_DEPTH:
                    # Recording it and stopping keeps the descent bounded. Descending anyway
                    # is what turned a deep tree into a RecursionError after the rename.
                    too_deep.add(relative)
                    continue
                descriptor = _open_child(child.name, dir_fd)
                try:
                    walk(descriptor, f'{relative}/', depth + 1)
                finally:
                    os.close(descriptor)
            elif kind == 'file':
                files.add(relative)
            else:
                specials.add(relative)

    walk(revision_fd, '', 0)
    return files, directories, symlinks, specials, too_deep


def _hash_handle(handle, path):
    """Return the sha256 of an open file, read in chunks so a large file is not held in memory."""
    digest = hashlib.sha256()
    try:
        for chunk in iter(lambda: handle.read(_HASH_CHUNK), b''):
            digest.update(chunk)
    except OSError as error:
        raise StorageError(f'Unable to read the stored file "{path}": {error}') from error
    return digest.hexdigest()


def write_staged_revision(project_root, storage_key, digest, files, manifest):
    """
    Write a revision's files to disk and return its revision directory.

    Content is written into a private staging directory and renamed onto the revision
    directory in one step, so a partial tree is never visible under revisions/. Each key is
    canonicalized here, so the stored tree matches the manifest by construction. An existing
    revision directory is treated as already written, which makes the call idempotent and
    makes a lost race against a concurrent writer of the same content a success.

    The stored tree is verified against the supplied manifest on every path that returns it,
    including a fresh write, so the postcondition is enforced rather than assumed. That costs
    one extra read and hash of the tree, bounded by the configured project size limit. The
    manifest is checked before any of that, which also confirms the digest naming the
    destination directory is the one that manifest computes to. A tree this call installed and
    then failed to verify is withdrawn, so one bad write does not poison a digest for every
    later retry. Raises UnsafePathError when two keys resolve to one canonical path
    (duplicate_path) or when a canonical path would leave the revision directory (escapes_root),
    RevisionCorruptError when the manifest or the stored tree does not hold up, and StorageError
    naming the surviving path when a tree that failed verification cannot be withdrawn.
    """
    validate_manifest(manifest, digest)
    destination = revision_directory(project_root, storage_key, digest)
    staged = staging_directory(project_root, storage_key, uuid.uuid4().hex)
    # paths owns the layout, so every component name is derived from it rather than spelled
    # out again here.
    key, revisions, revision_name = _storage_components(project_root, destination)
    _, staging, token = _storage_components(project_root, staged)

    with contextlib.ExitStack() as stack:
        root_fd = _open_root(stack, project_root)
        project_fd = _open_child_in(stack, key, root_fd, create=True)
        revisions_fd = _open_child_in(stack, revisions, project_fd, create=True)
        if _directory_exists(revisions_fd, revision_name):
            verify_revision_tree(project_root, storage_key, digest, manifest)
            return destination

        staging_fd = _open_child_in(stack, staging, project_fd, create=True)
        installed = False
        try:
            # No create=True: the token is a fresh uuid4, so an existing directory is a fault.
            os.mkdir(token, dir_fd=staging_fd)
            token_fd = _open_child_in(stack, token, staging_fd)
            _write_files(token_fd, files)
            try:
                os.rename(token, revision_name, src_dir_fd=staging_fd, dst_dir_fd=revisions_fd)
                installed = True
            except OSError:
                # Another writer of the same digest won the rename. Their tree is verified
                # below rather than assumed identical, since only the digest matched.
                if not _directory_exists(revisions_fd, revision_name):
                    raise
                shutil.rmtree(token, dir_fd=staging_fd)
            verify_revision_tree(project_root, storage_key, digest, manifest)
        except Exception as error:
            with contextlib.suppress(OSError):
                shutil.rmtree(token, dir_fd=staging_fd)
            if installed:
                # A successful rename consumes the token, so the failure above left this
                # call's own tree installed under the digest. A directory some other writer
                # installed is reported instead, never removed.
                _withdraw_failed_install(revisions_fd, revision_name, destination, error)
            raise
    return destination


def _withdraw_failed_install(revisions_fd, name, destination, error):
    """
    Remove a revision directory this call installed and could not verify, loudly if need be.

    Withdrawing it is part of failing: left in place it is found by every retry, verified, and
    failed on identically, so one bad write would poison a digest for good. When removal itself
    fails the directory is renamed aside instead, which needs only write permission on the parent
    and so survives a subtree whose contents cannot be unlinked. Either way the caller is told
    rather than left to guess, because the message reaches the revision's validation_errors,
    which is where an operator and a later reconciler look.

    Returns None when the directory is gone, which leaves the original failure to propagate.
    Raises StorageError carrying both that failure and this one when it is still there.
    """
    try:
        shutil.rmtree(name, dir_fd=revisions_fd)
        return
    except OSError as removal_error:
        quarantine = f'{name}{_QUARANTINE_SUFFIX}{uuid.uuid4().hex}'
        try:
            os.rename(name, quarantine, src_dir_fd=revisions_fd, dst_dir_fd=revisions_fd)
        except OSError as rename_error:
            logger.error(
                'Could not withdraw the unverified revision directory %s: %s, after removal failed with %s.',
                destination,
                rename_error,
                removal_error,
            )
            raise StorageError(
                f'{error} The revision directory "{destination}" could not be withdrawn '
                f'({removal_error}), so every retry of this digest fails until it is removed.'
            ) from error
        logger.error(
            'Could not remove the unverified revision directory %s (%s), so it was set aside as %s.',
            destination,
            removal_error,
            quarantine,
        )
        raise StorageError(
            f'{error} The revision directory "{destination}" could not be removed '
            f'({removal_error}) and was set aside as "{quarantine}".'
        ) from error


def _write_files(token_fd, files):
    """Write every source file into the staging directory under its canonical path."""
    seen = set()
    for path, content in files.items():
        canonical = normalize_source_path(path)
        if canonical in seen:
            raise UnsafePathError(path, 'duplicate_path', f'Multiple source files resolve to the path "{canonical}".')
        seen.add(canonical)
        _write_one_file(token_fd, path, canonical, content)


def _write_one_file(token_fd, original, canonical, content):
    """Create the directories one canonical path needs, then write its content."""
    *directories, name = canonical.split('/')
    if any(component in ('', '.', '..') for component in (*directories, name)):
        raise UnsafePathError(original, 'escapes_root', 'The resolved file path leaves the revision directory.')
    with contextlib.ExitStack() as stack:
        parent_fd = token_fd
        for component in directories:
            parent_fd = _open_child_in(stack, component, parent_fd, create=True)
        descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666, dir_fd=parent_fd)
        with os.fdopen(descriptor, 'wb') as handle:
            handle.write(content)


def delete_revision_directory(project_root, storage_key, digest):
    """Remove one revision's directory, tolerating a directory that is already gone."""
    key, revisions, name = _storage_components(project_root, revision_directory(project_root, storage_key, digest))
    with contextlib.ExitStack() as stack, contextlib.suppress(FileNotFoundError):
        root_fd = _open_root(stack, project_root)
        project_fd = _open_child_in(stack, key, root_fd)
        revisions_fd = _open_child_in(stack, revisions, project_fd)
        shutil.rmtree(name, dir_fd=revisions_fd)


def delete_project_directory(project_root, storage_key):
    """Remove a project's whole storage tree, tolerating a directory that is already gone."""
    (key,) = _storage_components(project_root, project_directory(project_root, storage_key))
    with contextlib.ExitStack() as stack, contextlib.suppress(FileNotFoundError):
        root_fd = _open_root(stack, project_root)
        shutil.rmtree(key, dir_fd=root_fd)
