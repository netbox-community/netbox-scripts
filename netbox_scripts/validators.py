from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

__all__ = ('data_paths_overlap', 'normalize_data_path')


def data_paths_overlap(path_a, path_b):
    """
    Return True if two canonical data paths overlap.

    Two paths overlap when they are identical or when one is a directory-level ancestor of
    the other. The empty path is the data source root, so it is an ancestor of every path.
    Comparison is segment-based, never a raw string prefix match, so 'automation/netbox' and
    'automation/netbox-old' are siblings rather than an overlap. Both arguments must already
    be in normalize_data_path() canonical form.
    """
    if path_a == path_b:
        return True
    segments_a = path_a.split('/') if path_a else []
    segments_b = path_b.split('/') if path_b else []
    shorter, longer = (segments_a, segments_b) if len(segments_a) <= len(segments_b) else (segments_b, segments_a)
    return longer[: len(shorter)] == shorter


def normalize_data_path(value):
    """
    Return the canonical form of a project data path.

    The canonical form is a POSIX-style relative directory path with single separators and
    no leading './' or trailing '/'. Empty input canonicalizes to '', which is reserved for
    upload projects, and the model decides whether that is permitted. Raises
    ValidationError for absolute paths, traversal segments, backslashes, and control
    characters. posixpath.normpath is deliberately not used here: it resolves '..' segments
    instead of rejecting them.
    """
    value = (value or '').strip()
    if not value:
        return ''
    # No code= on these: they surface through clean() and the serializer, where nothing branches on them.
    if '\\' in value:
        raise ValidationError(_('Backslashes are not allowed in data paths. Use forward slashes.'))
    if any(ord(char) < 32 or char == '\x7f' for char in value):
        raise ValidationError(_('Control characters are not allowed in data paths.'))
    if value.startswith('/'):
        raise ValidationError(_('Absolute paths are not allowed. Use a path relative to the data source root.'))
    segments = [segment for segment in value.split('/') if segment not in ('', '.')]
    if any(segment == '..' for segment in segments):
        raise ValidationError(_('Path traversal segments ("..") are not allowed.'))
    return '/'.join(segments)
