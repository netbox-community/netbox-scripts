__all__ = ('StorageConfigurationError', 'StorageError', 'UnsafePathError')


class StorageError(Exception):
    """Base class for every error raised by the project storage layer."""


class StorageConfigurationError(StorageError):
    """Raised when a required storage setting is missing or unusable."""


class UnsafePathError(StorageError):
    """
    Raised when a source path cannot be safely stored.

    The path attribute keeps the original input and the code attribute holds one of the
    fixed rejection codes, so a caller can branch on the reason without parsing the
    message.
    """

    def __init__(self, path, code, message):
        super().__init__(message)
        self.path = path
        self.code = code
