"""
Safe handling of project source paths and the on-disk storage layout.

Every function is a pure function of its arguments, so a stored file always hashes and
lands identically across hosts and regardless of any request or schema context.
"""

import re
import unicodedata
import uuid
from pathlib import Path, PureWindowsPath

from .exceptions import UnsafePathError

_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')
_STAGING_TOKEN = re.compile(r'^[0-9a-f]{32}$')

# Portability floors for a source path, deliberately fixed rather than configurable. These
# are not capacity limits to be tuned: a path that clears them has to remain writable, walkable
# and removable on every supported host, and importable by the loader that comes later.
# NAME_MAX is 255 bytes on ext4, XFS, and APFS. The whole-path bound leaves room for the
# project root and the revision digest to be prefixed while staying well under PATH_MAX. The
# depth bound sits far above any real Python package and keeps tree walking clear of both the
# descriptor and the recursion ceilings.
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 1024
MAX_PATH_DEPTH = 64


def normalize_source_path(path):
    """
    Return the canonical form of a source file path.

    The canonical form is a POSIX-style relative path with single separators,
    NFC-normalized, and no leading './'. The name is never trimmed. Leading or trailing
    whitespace and a trailing slash are rejected rather than rewritten, so a stored
    revision matches the source tree exactly. Raises UnsafePathError for whitespace,
    absolute or drive-qualified paths, traversal segments, backslashes, and control
    characters.

    A path that no host can materialize is rejected here too, under
    path_component_too_long, path_too_long, and path_too_deep. Those are content problems
    that fail identically on every retry, so they belong with the source rather than with the
    filesystem error they would otherwise become.
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

    canonical = '/'.join(segments)
    for segment in segments:
        length = len(segment.encode('utf-8'))
        if length > MAX_PATH_COMPONENT_BYTES:
            raise UnsafePathError(
                path,
                'path_component_too_long',
                f'"{segment[:40]}..." is {length} bytes, over the {MAX_PATH_COMPONENT_BYTES} byte limit for one '
                f'path component.',
            )
    total = len(canonical.encode('utf-8'))
    if total > MAX_PATH_BYTES:
        raise UnsafePathError(
            path, 'path_too_long', f'The path is {total} bytes, over the {MAX_PATH_BYTES} byte limit.'
        )
    if len(segments) > MAX_PATH_DEPTH:
        raise UnsafePathError(
            path,
            'path_too_deep',
            f'The path is {len(segments)} levels deep, over the {MAX_PATH_DEPTH} level limit.',
        )
    return canonical


def project_directory(project_root, storage_key):
    """Return the on-disk directory that holds every revision of one project."""
    try:
        key = str(uuid.UUID(str(storage_key)))
    except (ValueError, AttributeError, TypeError):
        raise UnsafePathError(str(storage_key), 'escapes_root', 'The storage key must be a UUID.') from None
    return Path(project_root) / key


def revision_directory(project_root, storage_key, digest):
    """Return the on-disk directory for one immutable revision of a project."""
    if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest):
        raise UnsafePathError(str(digest), 'escapes_root', 'The revision digest must be 64 lowercase hex characters.')
    return project_directory(project_root, storage_key) / 'revisions' / digest


def staging_directory(project_root, storage_key, token):
    """Return the scratch directory used while a revision is being written to disk."""
    if not isinstance(token, str) or not _STAGING_TOKEN.fullmatch(token):
        raise UnsafePathError(str(token), 'escapes_root', 'The staging token must be a 32-character hex string.')
    return project_directory(project_root, storage_key) / 'staging' / token
