"""
Error taxonomy for the project storage layer.

Every exception here roots at StorageError, so a caller can catch the whole tier in one clause.
"""

__all__ = (
    'ActivationError',
    'LimitExceededError',
    'RevisionCorruptError',
    'RevisionVanishedError',
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
    the offending file path.
    """

    # Ordered as UnsafePathError's, which has fourteen construction sites against this one.
    def __init__(self, path, code, message):
        super().__init__(message)
        self.code = code
        self.path = path


class ActivationError(StorageError):
    """Raised when a revision cannot become a project's active revision."""


class RevisionVanishedError(StorageError):
    """
    Raised when a revision's row was deleted while its content was being written.

    Deleting a project cascades its revisions away, and deletion takes no project lock, so a
    staging call already inside its write window can finish against rows that no longer exist.
    Its content is reclaimed by the cleanup the delete recorded, which rechecks references
    under the same lock, so the caller has nothing to undo and nothing to retry.
    """


class RevisionCorruptError(StorageError):
    """
    Raised when a stored revision tree does not match its manifest.

    The reasons attribute lists every mismatch found, so an operator sees the whole picture
    of a damaged tree rather than only the first problem.
    """

    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.reasons = tuple(reasons or ())
