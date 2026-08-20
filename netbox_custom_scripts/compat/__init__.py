"""
Legacy authoring imports for revision code.

Revision modules run with a builtins mapping whose __import__ resolves the name extras through a
plugin-owned stand-in. importlib.import_module('extras.scripts') never consults __import__ and is
the one form out of reach.
"""

import builtins
import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from types import ModuleType

from ..runtime.naming import PRIVATE_ROOT

__all__ = (
    'LEGACY_MODULES',
    'MIGRATION_HINTS',
    'compat_import',
    'install',
    'wrap_loader',
)

LEGACY_MODULES = {
    'extras.scripts': 'netbox_custom_scripts.scripts',
    'extras.reports': 'netbox_custom_scripts.compat.legacy',
}

MIGRATION_HINTS = {
    'extras.scripts': 'Import the authoring API from "netbox_custom_scripts.scripts" instead.',
    'extras.reports': 'Reports are not supported. Write a script importing "netbox_custom_scripts.scripts".',
}

_builtins_template = None
_extras_proxy = None


def install():
    """Install the revision import seam, at most once per process."""
    if not any(isinstance(finder, _RevisionFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _RevisionFinder())


def wrap_loader(loader):
    """Return a loader that seeds compat builtins, or the loader unchanged when it runs no source."""
    if isinstance(loader, _CompatSourceFileLoader):
        return loader
    if not isinstance(loader, importlib.machinery.SourceFileLoader):
        return loader
    return _CompatSourceFileLoader(loader.name, loader.path)


def compat_import(name, globals=None, locals=None, fromlist=(), level=0):
    """
    Import as usual, with the extras package resolved through this plugin's stand-in.

    Raises ImportError naming the migration once the host ships no module under a legacy name.
    """
    if level != 0 or not (name == 'extras' or name.startswith('extras.')):
        return builtins.__import__(name, globals, locals, fromlist, level)
    if name in LEGACY_MODULES:
        served = _serve_legacy(name)
        return served if fromlist else _extras_stand_in()
    imported = builtins.__import__(name, globals, locals, fromlist, level)
    # A bare extras, dotted or not, binds the stand-in so a later attribute reach lands here too.
    return imported if fromlist and name != 'extras' else _extras_stand_in()


class _CompatSourceFileLoader(importlib.machinery.SourceFileLoader):
    """Source loader that hands a module compat builtins before its body runs."""

    def exec_module(self, module):
        """Seed the mapping, then execute the module body."""
        # Its own copy, so one revision cannot reach another through a shared mapping.
        module.__dict__.setdefault('__builtins__', _compat_builtins())
        super().exec_module(module)


class _RevisionFinder(importlib.abc.MetaPathFinder):
    """Finder that wraps the loader of every module inside the private runtime namespace."""

    def find_spec(self, fullname, path=None, target=None):
        """Return the wrapped spec for a revision module, or None for every other name."""
        if not fullname.startswith(f'{PRIVATE_ROOT}.'):
            return None
        # Screened on the name alone: a path guard would let an unexpected None load a revision
        # module unwrapped, while delegating that path here finds nothing and fails honestly.
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is not None and spec.loader is not None:
            spec.loader = wrap_loader(spec.loader)
        return spec


def _compat_builtins():
    """Return a private builtins mapping for one revision module."""
    global _builtins_template
    if _builtins_template is None:
        _builtins_template = {**vars(builtins), '__import__': compat_import}
    return dict(_builtins_template)


def _host_provides(legacy):
    """Return whether the host still ships a module of its own under one legacy name."""
    try:
        return importlib.util.find_spec(legacy) is not None
    except (ImportError, ValueError):
        return False


def _serve_legacy(legacy):
    """Return the plugin module serving one legacy name, or refuse once the host has dropped it."""
    if not _host_provides(legacy):
        raise ImportError(f'"{legacy}" is no longer part of NetBox. {MIGRATION_HINTS[legacy]}', name=legacy)
    return importlib.import_module(LEGACY_MODULES[legacy])


def _extras_stand_in():
    """Return the module that stands in for the extras package inside revision code."""
    global _extras_proxy
    if _extras_proxy is None:
        proxy = ModuleType('extras')

        # No setattr for the legacy names, so each reach re-probes and one proxy serves both eras.
        def __getattr__(attribute):
            if f'extras.{attribute}' in LEGACY_MODULES:
                return _serve_legacy(f'extras.{attribute}')
            try:
                return getattr(importlib.import_module('extras'), attribute)
            except AttributeError:
                return importlib.import_module(f'extras.{attribute}')

        proxy.__getattr__ = __getattr__
        _extras_proxy = proxy
    return _extras_proxy
