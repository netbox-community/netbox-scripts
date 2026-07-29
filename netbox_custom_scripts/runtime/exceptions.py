from netbox_custom_scripts.storage.exceptions import StorageError, UnsafePathError

__all__ = (
    'EntrypointImportError',
    'InvalidModulePathError',
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


class InvalidModulePathError(UnsafePathError):
    """Raised when a source path names something Python could never import."""


class EntrypointImportError(Exception):
    """
    Raised when an entrypoint cannot be imported from its revision tree.

    The detail attribute keeps a structured record of the failure (path, code, message,
    exception type, traceback) and __cause__ carries the original exception whenever one
    exists, so the validation layer classifies outcomes without parsing messages.
    """

    def __init__(self, message, detail):
        super().__init__(message)
        self.detail = dict(detail)
