import keyword

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

__all__ = (
    'data_source_relative_path',
    'source_path_to_dotted_name',
)


def data_source_relative_path(path, data_path):
    """
    Return a Data Source file's path relative to data_path, or None if it sits outside.

    An empty data_path takes the whole Data Source. A file whose path is the directory itself is
    outside it, because a project's source is what the directory contains.
    """
    prefix = data_path.split('/') if data_path else []
    segments = path.split('/')
    # Segment-wise, so "automation/netbox" does not claim "automation/netbox-old".
    if segments[: len(prefix)] != prefix or len(segments) == len(prefix):
        return None
    return '/'.join(segments[len(prefix) :])


def source_path_to_dotted_name(path):
    """
    Return the dotted module name a canonical source path imports as.

    'tools/deploy.py' maps to 'tools.deploy' and 'pkg/__init__.py' to 'pkg'. A bare
    '__init__.py' names the project root package rather than a separate script file. Every
    segment must be a valid Python identifier and not a reserved keyword, or no loader
    could ever import the module this path names. Raises ValidationError naming the
    problem.
    """
    # Every code here is read rather than displayed: naming.py rethrows it and validation records it.
    if not path.endswith('.py'):
        raise ValidationError(
            _('A script file must be a Python module file ending in ".py".'),
            code='not_a_python_file',
        )
    segments = path[: -len('.py')].split('/')
    if segments[-1] == '__init__':
        segments = segments[:-1]
        if not segments:
            raise ValidationError(
                _('The root "__init__.py" names the project package itself, not a script file.'),
                code='root_script_file',
            )
    for segment in segments:
        if not segment.isidentifier():
            raise ValidationError(
                _('"{segment}" is not a valid Python identifier, so this path cannot be imported.').format(
                    segment=segment
                ),
                code='invalid_identifier',
            )
        if keyword.iskeyword(segment):
            raise ValidationError(
                _('"{segment}" is a reserved Python keyword, so this path cannot be imported.').format(segment=segment),
                code='reserved_keyword',
            )
    return '.'.join(segments)
