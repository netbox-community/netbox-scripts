"""
Accessors for the plugin's storage settings.

This module owns settings resolution for the storage package. Every value is read through
get_plugin_config on each call, so a setting overridden in a test with override_settings
takes effect immediately. Path settings are validated lazily, when a storage function first
needs them, rather than through a Django system check, so the plugin still boots on a
deployment that has not configured storage yet. A caller resolves the limits once with
get_storage_limits and passes the result down.
"""

import dataclasses
import os
import pathlib

from netbox.plugins import get_plugin_config

from .. import constants
from .exceptions import StorageConfigurationError

_PLUGIN_NAME = 'netbox_custom_scripts'


def _resolve_directory(parameter):
    value = get_plugin_config(_PLUGIN_NAME, parameter)
    if not value:
        raise StorageConfigurationError(f'The {parameter} storage setting is not configured. Set it in PLUGINS_CONFIG.')
    if not isinstance(value, (str, os.PathLike)):
        raise StorageConfigurationError(f'The {parameter} storage setting must be an absolute filesystem path.')
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise StorageConfigurationError(f'The {parameter} storage setting must be an absolute path.')
    if not path.is_dir():
        raise StorageConfigurationError(f'The {parameter} storage path does not exist or is not a directory.')
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise StorageConfigurationError(f'The {parameter} storage path must be readable, writable, and traversable.')
    return path


def get_project_root():
    """Return the configured root directory for project source storage."""
    return _resolve_directory('project_root')


def get_runtime_cache_root():
    """Return the configured root directory for the runtime script cache."""
    return _resolve_directory('runtime_cache_root')


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
