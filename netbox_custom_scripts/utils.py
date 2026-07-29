import keyword

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _

__all__ = ('source_path_to_dotted_name',)


def source_path_to_dotted_name(path):
    """
    Return the dotted module name a canonical source path imports as.

    'tools/deploy.py' maps to 'tools.deploy' and 'pkg/__init__.py' to 'pkg'. A bare
    '__init__.py' names the project root package rather than a separate entrypoint. Every
    segment must be a valid Python identifier and not a reserved keyword, or no loader
    could ever import the module this path names. Raises ValidationError naming the
    problem.
    """
    if not path.endswith('.py'):
        raise ValidationError(
            _('An entrypoint must be a Python module file ending in ".py".'),
            code='not_a_python_file',
        )
    segments = path[: -len('.py')].split('/')
    if segments[-1] == '__init__':
        segments = segments[:-1]
        if not segments:
            raise ValidationError(
                _('The root "__init__.py" names the project package itself, not an entrypoint.'),
                code='root_entrypoint',
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
