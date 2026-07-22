from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

__all__ = ('normalize_data_path',)


def normalize_data_path(value):
    """
    Return the canonical form of a project data path: a POSIX-style relative
    directory path with single separators and no leading './' or trailing '/'.
    Empty input canonicalizes to '' (reserved for upload projects); the model
    decides whether that is permitted. Raises ValidationError for absolute
    paths, traversal segments, backslashes, and control characters.
    posixpath.normpath is deliberately not used here: it resolves '..'
    segments instead of rejecting them.
    """
    value = (value or '').strip()
    if not value:
        return ''
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
