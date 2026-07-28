from netbox_custom_scripts.storage.exceptions import StorageError

__all__ = (
    'LocalCacheCorruptError',
    'LocalCacheError',
)


class LocalCacheError(StorageError):
    """Raised when the runtime cache cannot produce or protect a usable local tree."""


class LocalCacheCorruptError(StorageError):
    """
    Raised when a cached revision tree does not match its revision manifest.

    The reasons attribute lists every mismatch found, so an operator sees the whole picture
    of a damaged tree rather than only the first problem. The cache consumes this internally
    to set a failed tree aside and rebuild it, so reaching a caller means rebuilding did not
    settle it.
    """

    def __init__(self, message, reasons=None):
        super().__init__(message)
        self.reasons = tuple(reasons or ())
