"""
Private module names for revision imports.

Revision code never imports under a name an author chose. Everything sits below one
private root, with one container per project and one package per revision, so two
projects, two revisions, and installed distributions can never collide. The name
components are storage identities that never change, which is what lets a revision be
unloaded and re-imported deterministically.
"""

import uuid

from django.core.exceptions import ValidationError

from ..utils import source_path_to_dotted_name
from .exceptions import InvalidModulePathError

__all__ = (
    'PRIVATE_ROOT',
    'project_module_name',
    'revision_module_name',
    'script_file_dotted_name',
)

PRIVATE_ROOT = '_netbox_scripts_runtime'


def project_module_name(storage_key):
    """Return the container module name holding every loaded revision of one project."""
    return f'{PRIVATE_ROOT}.p_{uuid.UUID(str(storage_key)).hex}'


def revision_module_name(storage_key, digest):
    """Return the package name one revision's tree imports under."""
    return f'{project_module_name(storage_key)}.r_{digest}'


def script_file_dotted_name(path):
    """
    Return the dotted name an entrypoint occupies below its revision package.

    Wraps the shared converter so loader callers deal in one exception family: a path the
    converter rejects raises InvalidModulePathError carrying the converter's code.
    """
    try:
        return source_path_to_dotted_name(path)
    except ValidationError as error:
        raise InvalidModulePathError(path=path, code=error.code, message=error.messages[0]) from error
