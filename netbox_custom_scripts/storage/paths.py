"""
Safe handling of project source paths and the on-disk storage layout.

This module imports no Django ORM. It owns source-path safety, the on-disk layout, and
walking a source directory with bounded reads. Path construction is a pure function of its
arguments and the configured project root, so a stored file always hashes and lands
identically across hosts and regardless of any request or schema context.
"""

import os
import re
import stat
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

from . import config
from .exceptions import LimitExceededError, StorageError, UnsafePathError

_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_STAGING_TOKEN = re.compile(r'^[0-9a-f]{32}$')


def normalize_source_path(path):
    """
    Return the canonical form of a source file path.

    The canonical form is a POSIX-style relative path with single separators,
    NFC-normalized, and no leading './'. The name is never trimmed. Leading or trailing
    whitespace and a trailing slash are rejected rather than rewritten, so a stored
    revision matches the source tree exactly. Raises UnsafePathError for whitespace,
    absolute or drive-qualified paths, traversal segments, backslashes, and control
    characters.
    """
    normalized = unicodedata.normalize('NFC', path)
    if normalized != normalized.strip():
        raise UnsafePathError(path, 'path_traversal', 'Leading or trailing whitespace is not allowed in source paths.')
    # Backslashes and control characters are rejected under the path_traversal code. Both
    # are ways to escape the intended directory: a backslash acts as an alternate separator
    # on some hosts, and control characters can confuse a path parser or truncate a name.
    if '\\' in normalized:
        raise UnsafePathError(
            path, 'path_traversal', 'Backslashes are not allowed in source paths. Use forward slashes.'
        )
    if any(ord(char) < 32 or char == '\x7f' for char in normalized):
        raise UnsafePathError(path, 'path_traversal', 'Control characters are not allowed in source paths.')
    if normalized.startswith('/') or PureWindowsPath(normalized).drive:
        raise UnsafePathError(path, 'absolute_path', 'Absolute and drive-qualified source paths are not allowed.')
    if normalized.endswith('/'):
        raise UnsafePathError(path, 'path_traversal', 'Source paths must reference a file, not a directory.')
    segments = [segment for segment in normalized.split('/') if segment not in ('', '.')]
    if any(segment == '..' for segment in segments):
        raise UnsafePathError(path, 'path_traversal', 'Path traversal segments ("..") are not allowed.')
    if not segments:
        raise UnsafePathError(path, 'path_traversal', 'Source paths must reference a file within the project.')
    return '/'.join(segments)


def project_directory(storage_key):
    """Return the on-disk directory that holds every revision of one project."""
    try:
        key = str(uuid.UUID(str(storage_key)))
    except (ValueError, AttributeError, TypeError):
        raise UnsafePathError(str(storage_key), 'escapes_root', 'The storage key must be a UUID.') from None
    return config.get_project_root() / key


def revision_directory(storage_key, digest):
    """Return the on-disk directory for one immutable revision of a project."""
    if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest):
        raise UnsafePathError(str(digest), 'escapes_root', 'The revision digest must be 64 lowercase hex characters.')
    return project_directory(storage_key) / 'revisions' / digest


def staging_directory(storage_key, token):
    """Return the scratch directory used while a revision is being written to disk."""
    if not isinstance(token, str) or not _STAGING_TOKEN.fullmatch(token):
        raise UnsafePathError(str(token), 'escapes_root', 'The staging token must be a 32-character hex string.')
    return project_directory(storage_key) / 'staging' / token


def _read_regular_file(candidate, relative, max_file_size):
    """
    Return the content of one directory entry, read once through a validated descriptor.

    The entry is opened without following a final symbolic link, the opened descriptor is
    confirmed to be a regular file, and at most max_file_size + 1 bytes are read so a
    growing or replaced file cannot exceed the configured memory bound. Raises
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


def iter_directory_files(root):
    """
    Yield (raw_relative_posix_path, content_bytes) for every regular file under root.

    The tree is walked top down and in sorted order within each directory. Paths are
    yielded unmodified so the manifest builder owns normalization and duplicate detection.
    The configured per-file, file-count, and total-size limits are enforced as the tree is
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

    max_file_size = config.get_max_file_size()
    max_file_count = config.get_max_file_count()
    max_project_size = config.get_max_project_size()
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
            if count > max_file_count:
                raise LimitExceededError('too_many_files', f'The source tree has more than {max_file_count} files.')
            content = _read_regular_file(candidate, relative, max_file_size)
            total += len(content)
            if total > max_project_size:
                raise LimitExceededError(
                    'project_too_large', f'The source tree exceeds the {max_project_size} byte limit.'
                )
            yield relative, content
