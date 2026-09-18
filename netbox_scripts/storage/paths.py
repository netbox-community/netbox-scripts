"""
Safe handling of project source paths and the storage layout built from them.

Every function is a pure function of its arguments, so a stored file always hashes and
lands identically across hosts and regardless of any request or schema context.
"""

import re
import unicodedata
import uuid
from pathlib import PureWindowsPath

from django.utils.translation import gettext_lazy as _

from .exceptions import UnsafePathError

__all__ = (
    'MAX_PATH_BYTES',
    'MAX_PATH_COMPONENT_BYTES',
    'MAX_PATH_DEPTH',
    'STORAGE_PREFIX',
    'case_insensitive_collisions',
    'case_insensitive_nodes',
    'normalize_source_path',
    'project_prefix',
    'revision_key',
    'revision_prefix',
)

_HEX_DIGEST = re.compile(r'^[0-9a-f]{64}$')

# Portability floors for a source path, deliberately fixed rather than configurable. These
# are not capacity limits to be tuned: a path that clears them has to remain writable, walkable
# and removable on every supported host, and importable by the loader that comes later.
# NAME_MAX is 255 bytes on ext4, XFS, and APFS. The whole-path bound budgets for the complete
# object key: S3 caps it at 1024 UTF-8 bytes including every prefix, this plugin's own prefix
# spends 127 of them (netbox-scripts/ 15, project UUID and slash 37, revisions/ 10,
# digest and slash 65), and 768 for the source path leaves 129 for an operator's backend
# location. One fixed floor beats a per-backend policy engine. The depth bound sits far above
# any real Python package and keeps walking a stored revision clear of the recursion ceiling.
MAX_PATH_COMPONENT_BYTES = 255
MAX_PATH_BYTES = 768
MAX_PATH_DEPTH = 64

# Refused as source: a compiled file imports without the source anyone would review.
_COMPILED_SUFFIXES = ('.pyc', '.pyo')
_BYTECODE_DIRECTORY = '__pycache__'

# Every key this plugin stores sits under one prefix, so project source stays apart from
# anything else on the backend, including on an entry an operator deliberately pointed at
# the same bucket or directory that holds NetBox's media.
STORAGE_PREFIX = 'netbox-scripts'


def normalize_source_path(path):
    """
    Return the canonical form of a source file path.

    The canonical form is a POSIX-style relative path with single separators,
    NFC-normalized, and no leading './'. The name is never trimmed. Leading or trailing
    whitespace and a trailing slash are rejected rather than rewritten, so a stored
    revision matches the source tree exactly. Raises UnsafePathError for whitespace,
    absolute or drive-qualified paths, traversal segments, backslashes, and control
    characters.

    Compiled Python artifacts are rejected under compiled_artifact, because they are not
    source and cannot be reviewed as source.

    A path that no host can materialize is rejected here too, under
    path_component_too_long, path_too_long, and path_too_deep. Those are content problems
    that fail identically on every retry, so they belong with the source rather than with the
    filesystem error they would otherwise become.
    """
    normalized = unicodedata.normalize('NFC', path)
    if normalized != normalized.strip():
        raise UnsafePathError(
            path=path,
            code='path_traversal',
            message=_('Leading or trailing whitespace is not allowed in source paths.'),
        )
    # Backslashes and control characters are rejected under the path_traversal code. Both
    # are ways to escape the intended directory: a backslash acts as an alternate separator
    # on some hosts, and control characters can confuse a path parser or truncate a name.
    if '\\' in normalized:
        raise UnsafePathError(
            path=path,
            code='path_traversal',
            message=_('Backslashes are not allowed in source paths. Use forward slashes.'),
        )
    if any(ord(char) < 32 or char == '\x7f' for char in normalized):
        raise UnsafePathError(
            path=path, code='path_traversal', message=_('Control characters are not allowed in source paths.')
        )
    if normalized.startswith('/') or PureWindowsPath(normalized).drive:
        raise UnsafePathError(
            path=path, code='absolute_path', message=_('Absolute and drive-qualified source paths are not allowed.')
        )
    if normalized.endswith('/'):
        raise UnsafePathError(
            path=path, code='path_traversal', message=_('Source paths must reference a file, not a directory.')
        )
    segments = [segment for segment in normalized.split('/') if segment not in ('', '.')]
    if any(segment == '..' for segment in segments):
        raise UnsafePathError(
            path=path, code='path_traversal', message=_('Path traversal segments ("..") are not allowed.')
        )
    if not segments:
        raise UnsafePathError(
            path=path,
            code='path_traversal',
            message=_('Source paths must reference a file within the project.'),
        )
    # Refusing only the lower-case spelling stores opaque bytecode as reviewable source.
    # Import never reaches it here, so this bounds what the tree holds, not what runs.
    folded = [segment.lower() for segment in segments]
    if folded[-1].endswith(_COMPILED_SUFFIXES) or _BYTECODE_DIRECTORY in folded:
        raise UnsafePathError(
            path=path,
            code='compiled_artifact',
            message=_('Compiled Python files and "{directory}" directories are not stored as project source.').format(
                directory=_BYTECODE_DIRECTORY
            ),
        )

    canonical = '/'.join(segments)
    for segment in segments:
        length = len(segment.encode('utf-8'))
        if length > MAX_PATH_COMPONENT_BYTES:
            raise UnsafePathError(
                path=path,
                code='path_component_too_long',
                message=_(
                    '"{segment}..." is {length} bytes, over the {limit} byte limit for one path component.'
                ).format(segment=segment[:40], length=length, limit=MAX_PATH_COMPONENT_BYTES),
            )
    total = len(canonical.encode('utf-8'))
    if total > MAX_PATH_BYTES:
        raise UnsafePathError(
            path=path,
            code='path_too_long',
            message=_('The path is {total} bytes, over the {limit} byte limit.').format(
                total=total, limit=MAX_PATH_BYTES
            ),
        )
    if len(segments) > MAX_PATH_DEPTH:
        raise UnsafePathError(
            path=path,
            code='path_too_deep',
            message=_('The path is {depth} levels deep, over the {limit} level limit.').format(
                depth=len(segments), limit=MAX_PATH_DEPTH
            ),
        )
    return canonical


def case_insensitive_nodes(path):
    """
    Return every tree node one path contributes, keyed by its case-insensitive form.

    A node is the path or one of its ancestor directories. Comparison is str.lower(), not
    casefold: APFS, NTFS, and HFS+ use simple case mapping, and folding would merge names
    they keep apart, such as "strasse.py" and "straße.py".
    """
    segments = path.split('/')
    nodes = ('/'.join(segments[:index]) for index in range(1, len(segments) + 1))
    return {node.lower(): node for node in nodes}


def case_insensitive_collisions(paths):
    """
    Map each path touching a case-insensitive collision to the two node names that collide.

    A file and a directory sharing one exact name is a plain path conflict, not this. The
    reported node is the shallowest that collides, which is where a rename resolves it.
    """
    paths = sorted(paths)
    names_by_form = {}
    for path in paths:
        for form, node in case_insensitive_nodes(path).items():
            names_by_form.setdefault(form, set()).add(node)
    collided = {form for form, names in names_by_form.items() if len(names) > 1}

    rejected = {}
    for path in paths:
        for form, node in case_insensitive_nodes(path).items():
            if form in collided:
                rejected[path] = (node, min(name for name in names_by_form[form] if name != node))
                break
    return rejected


def _storage_key_component(storage_key):
    """Return one project's storage key as the canonical UUID string that names its content."""
    try:
        return str(uuid.UUID(str(storage_key)))
    except (ValueError, AttributeError, TypeError):
        raise UnsafePathError(
            path=str(storage_key), code='escapes_root', message=_('The storage key must be a UUID.')
        ) from None


def _digest_component(digest):
    """Return one revision's digest, confirmed to be the 64 lowercase hex characters it must be."""
    if not isinstance(digest, str) or not _HEX_DIGEST.fullmatch(digest):
        raise UnsafePathError(
            path=str(digest),
            code='escapes_root',
            message=_('The revision digest must be 64 lowercase hex characters.'),
        )
    return digest


def project_prefix(storage_key):
    """Return the storage key prefix holding every stored revision of one project."""
    return f'{STORAGE_PREFIX}/{_storage_key_component(storage_key)}/'


def revision_prefix(storage_key, digest):
    """Return the storage key prefix holding one immutable revision's content."""
    return f'{project_prefix(storage_key)}revisions/{_digest_component(digest)}/'


def revision_key(storage_key, digest, path):
    """
    Return the storage key of one file inside a stored revision.

    The path is canonicalized here rather than taken on trust, so one key names one object
    however the caller spelled the path, and every rule normalize_source_path enforces applies
    to a key handed to a backend this plugin does not control.
    """
    return f'{revision_prefix(storage_key, digest)}{normalize_source_path(path)}'
