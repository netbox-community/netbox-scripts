__all__ = ('StorageConfigurationError', 'StorageError')


class StorageError(Exception):
    """Base class for every error raised by the project storage layer."""


class StorageConfigurationError(StorageError):
    """Raised when a required storage setting is missing or unusable."""
