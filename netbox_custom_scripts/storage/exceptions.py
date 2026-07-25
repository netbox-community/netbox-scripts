__all__ = (
    'ActivationError',
    'LimitExceededError',
    'RevisionCorruptError',
    'StorageConfigurationError',
    'StorageError',
    'UnsafePathError',
)


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


class LimitExceededError(StorageError):
    """
    Raised when a source file or project would exceed a configured storage limit.

    The code attribute holds one of the fixed rejection codes and the path attribute holds
    the offending file path, or None for a project-wide limit.
    """

    def __init__(self, code, message, path=None):
        super().__init__(message)
        self.code = code
        self.path = path


class ActivationError(StorageError):
    """Raised when a revision cannot become a project's active revision."""


class RevisionCorruptError(StorageError):
    """
    Raised when a stored revision tree does not match its manifest.

    The reasons attribute lists every mismatch found, so an operator sees the whole picture
    of a damaged tree rather than only the first problem.
    """

    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.reasons = tuple(reasons or ())
