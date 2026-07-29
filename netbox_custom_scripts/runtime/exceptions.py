"""
Error taxonomy for the runtime tier.

The cache errors root in the storage hierarchy because a tree the cache cannot produce is a
failure to deliver stored content. Import and discovery errors do not, because they are
statements about revision code rather than about storage.
"""

from ..storage.exceptions import StorageError, UnsafePathError

__all__ = (
    'DiscoveryError',
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


class DiscoveryError(Exception):
    """
    Raised when an imported entrypoint declares an invalid script publication.

    A content statement about the revision: the module imported fine but its script_order
    or class layout breaks the publication contract. The code attribute holds one of the
    fixed rejection codes and name identifies the offending class where one exists.
    """

    def __init__(self, message, code, name=None):
        super().__init__(message)
        self.code = code
        self.name = name
