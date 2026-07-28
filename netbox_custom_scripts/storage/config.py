"""
Accessors for the plugin's storage settings.

This module owns settings resolution for the storage package. Every value is read through
get_plugin_config on each call, so a setting overridden in a test with override_settings
takes effect immediately. A caller resolves the limits once with get_storage_limits and
passes the result down.

Revision content lives in a Django storage backend, resolved here through one alias so that a
single call site decides where a deployment keeps it.
"""

import dataclasses

from django.conf import settings
from django.core import checks
from django.core.files.storage import storages

from netbox.plugins import get_plugin_config

from .. import constants
from .exceptions import StorageConfigurationError

_PLUGIN_NAME = 'netbox_custom_scripts'

# The STORAGES alias holding Custom Script Project source. The entry is required rather than
# falling back to the default alias, because revision content is executable source and must
# not silently inherit the visibility, retention, and sharing policy an operator chose for
# ordinary media. Pointing this alias at the same physical backend as the default is a
# legitimate decision, made visible by writing it out, and it lets the two diverge later.
STORAGE_ALIAS = 'netbox_custom_scripts'


def get_storage():
    """
    Return the Django storage backend holding every project's revision content.

    The backend comes from the dedicated STORAGE_ALIAS entry in STORAGES. Resolved on each
    call, so a backend swapped in a test takes effect immediately. Django caches the
    constructed backend per alias and rebuilds it when STORAGES changes, so this stays cheap.

    Raises StorageConfigurationError when the alias is missing or its backend cannot be
    constructed, which makes every storage operation fail closed on an unconfigured
    deployment while NetBox itself still boots. Deletion cleanup records the same error on
    its job instead of failing inside a commit callback.
    """
    if STORAGE_ALIAS not in settings.STORAGES:
        raise StorageConfigurationError(
            f'Project storage is not configured. Define a "{STORAGE_ALIAS}" entry in the STORAGES setting.'
        )
    try:
        return storages[STORAGE_ALIAS]
    except Exception as error:
        raise StorageConfigurationError(
            f'The "{STORAGE_ALIAS}" entry in the STORAGES setting could not be used for project storage: {error}'
        ) from error


def check_storage_configured(app_configs, **kwargs):
    """
    Report a missing project storage entry where an administrator will see it.

    A Warning rather than an Error keeps migrate and runserver usable on an installation that
    has not configured the plugin yet. The storage operations themselves fail closed either
    way, through the StorageConfigurationError get_storage() raises.
    """
    if STORAGE_ALIAS in settings.STORAGES:
        return []
    return [
        checks.Warning(
            'Custom Script Project storage is not configured. Revision staging, activation, '
            'and cleanup are refused until it is.',
            hint=f'Define a "{STORAGE_ALIAS}" entry in the STORAGES setting.',
            id='netbox_custom_scripts.W001',
        )
    ]


def _resolve_positive_integer(parameter, default):
    value = get_plugin_config(_PLUGIN_NAME, parameter, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StorageConfigurationError(f'The {parameter} storage setting must be a positive integer.')
    return value


def get_max_file_size():
    """Return the maximum accepted size in bytes for a single source file."""
    return _resolve_positive_integer('max_file_size', constants.DEFAULT_MAX_FILE_SIZE)


def get_max_project_size():
    """Return the maximum accepted total size in bytes for a project's source tree."""
    return _resolve_positive_integer('max_project_size', constants.DEFAULT_MAX_PROJECT_SIZE)


def get_max_file_count():
    """Return the maximum accepted number of files in a project's source tree."""
    return _resolve_positive_integer('max_file_count', constants.DEFAULT_MAX_FILE_COUNT)


@dataclasses.dataclass(frozen=True)
class StorageLimits:
    """The storage limits that apply to one staging operation."""

    max_file_size: int
    max_project_size: int
    max_file_count: int


def get_storage_limits():
    """
    Return the configured storage limits as one immutable value.

    A caller resolves this once per staging operation and passes it down, so every check in
    that operation sees the same limits even if the settings change while it runs.
    """
    return StorageLimits(
        max_file_size=get_max_file_size(),
        max_project_size=get_max_project_size(),
        max_file_count=get_max_file_count(),
    )
