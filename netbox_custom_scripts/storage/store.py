"""
Filesystem access to a project's source tree.

This module imports no Django ORM and reads no configuration. It walks a source directory
in a stable order, enforcing the limits it is handed as the tree is read, so an oversized
tree is rejected without being loaded into memory.
"""

import os
import stat
from pathlib import Path

from .exceptions import LimitExceededError, StorageError, UnsafePathError


def _read_regular_file(candidate, relative, max_file_size):
    """
    Return the content of one directory entry, read once through a validated descriptor.

    The entry is opened without following a final symbolic link, the opened descriptor is
    confirmed to be a regular file, and at most max_file_size + 1 bytes are read so a
    growing or replaced file cannot exceed the supplied memory bound. Raises
    UnsafePathError (special_file) for a non-regular file, LimitExceededError
    (file_too_large) when the content is over the limit, and StorageError for any
    operating-system read failure.
    """
    try:
        descriptor = os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise UnsafePathError(str(candidate), 'special_file', 'Special files are not allowed in a source tree.')
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

    The tree is walked top down and in sorted order within each directory. Paths are
    yielded unmodified so the manifest builder owns normalization and duplicate detection.
    The supplied per-file, file-count, and total-size limits are enforced as the tree is
    read, and each file is opened once and read with a bound, so an oversized tree is
    rejected without being loaded into memory. Raises UnsafePathError on a symbolic link
    (symlink) or a special file (special_file), StorageError when the root is missing, is
    not a directory, or cannot be read, and LimitExceededError when a limit is exceeded.
    """
    root = Path(root)
    if root.is_symlink():
        raise UnsafePathError(str(root), 'symlink', 'The source tree root must not be a symbolic link.')
    if not root.exists():
        raise StorageError(f'The source tree root does not exist: {root}')
    if not root.is_dir():
        raise StorageError(f'The source tree root is not a directory: {root}')

    count = 0
    total = 0

    def raise_walk_error(error):
        raise StorageError(f'Unable to read the source tree: {error}') from error

    for current, dirnames, filenames in os.walk(root, followlinks=False, onerror=raise_walk_error):
        dirnames.sort()
        filenames.sort()
        current = Path(current)
        for dirname in dirnames:
            candidate = current / dirname
            if candidate.is_symlink():
                raise UnsafePathError(str(candidate), 'symlink', 'Symbolic links are not allowed in a source tree.')
        for filename in filenames:
            candidate = current / filename
            if candidate.is_symlink():
                raise UnsafePathError(str(candidate), 'symlink', 'Symbolic links are not allowed in a source tree.')
            relative = candidate.relative_to(root).as_posix()
            count += 1
            if count > limits.max_file_count:
                raise LimitExceededError(
                    'too_many_files', f'The source tree has more than {limits.max_file_count} files.'
                )
            content = _read_regular_file(candidate, relative, limits.max_file_size)
            total += len(content)
            if total > limits.max_project_size:
                raise LimitExceededError(
                    'project_too_large', f'The source tree exceeds the {limits.max_project_size} byte limit.'
                )
            yield relative, content
