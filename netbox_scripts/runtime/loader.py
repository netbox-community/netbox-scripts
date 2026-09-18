"""
Import machinery for materialized revision trees.

Revision code imports below the private namespace naming.py defines: a root and one
container per project that are synthetic packages holding no code, and one real package
per revision rooted at its verified cache tree. Nothing here touches sys.path, the working
directory, or the global finder chain, so the host process sees project code only under
names it can never collide with, and installed distributions always win an absolute
import.

Project code first runs inside one failure boundary that snapshots the revision's
sys.modules footprint, so a failed import sweeps every module it managed to register,
including helpers a root __init__ pulled in before dying. Callers name the worker-control
exception types that must escape untouched, and everything else project code raises
arrives as ScriptFileImportError with the original chained.

One process lock guards the shared containers, and one reentrant lock per revision
serializes import, discovery, and unload. revision_import_session holds the latter across
a whole validation, because two revision rows can share one source digest and therefore
one namespace. Cross-process coordination stays with the cache, a namespace is process
state.
"""

import importlib
import importlib.machinery
import importlib.util
import sys
import threading
import traceback
from contextlib import contextmanager

from django.utils.translation import gettext_lazy as _

from ..compat import wrap_loader
from ..storage.manifest import validate_manifest
from .cache import materialize_revision
from .exceptions import ScriptFileImportError
from .naming import PRIVATE_ROOT, project_module_name, revision_module_name, script_file_dotted_name

__all__ = (
    'import_script_file',
    'revision_import_session',
    'unload_revision',
)

_namespace_lock = threading.Lock()
_revision_locks = {}
_revision_locks_guard = threading.Lock()


def import_script_file(storage_key, digest, script_file_path, *, storage, manifest, passthrough=(), cache_root=None):
    """
    Import one script file from a revision and return its module.

    The manifest is validated before anything reads it, the path is mapped to its dotted
    name and checked against the manifest before any I/O, the tree comes from the cache
    fully verified, and only then does project code run, inside the failure boundary
    described in the module docstring. passthrough lists exception types that re-raise
    unchanged after the sweep, as KeyboardInterrupt and GeneratorExit always do. Raises
    InvalidModulePathError for a path that could never import, RevisionCorruptError for a
    manifest that cannot be trusted, ScriptFileImportError when the path is not part of the
    manifest or the import fails, and whatever materialization raises when storage or cache
    cannot deliver.
    """
    dotted = script_file_dotted_name(script_file_path)
    # The membership check below reads the manifest, so it is confirmed before that read.
    validate_manifest(manifest, digest)
    manifest_paths = {entry['path'] for entry in manifest}
    if script_file_path not in manifest_paths:
        message = _('The script file "{path}" is not part of the revision manifest.').format(path=script_file_path)
        raise ScriptFileImportError(
            message,
            {
                'path': script_file_path,
                'code': 'script_file_not_in_manifest',
                'message': message,
                'exception_type': None,
                'traceback': None,
            },
        ) from None
    project_name = _ensure_parent_packages(storage_key)
    revision_name = revision_module_name(storage_key, digest)
    with _revision_lock(storage_key, digest):
        # Inside the lock: materialization can rename a damaged tree out from under an importer.
        revision_dir = materialize_revision(storage, storage_key, digest, manifest, cache_root=cache_root)
        importlib.invalidate_caches()
        before = _loaded_names(revision_name)
        try:
            if revision_name not in sys.modules:
                _register_revision_package(
                    revision_name, revision_dir, '__init__.py' in manifest_paths, sys.modules[project_name]
                )
            return importlib.import_module(f'{revision_name}.{dotted}')
        except BaseException as error:
            _sweep_added(revision_name, before, sys.modules.get(project_name))
            if isinstance(error, (*passthrough, KeyboardInterrupt, GeneratorExit)):
                raise
            raise ScriptFileImportError(
                _('The script file "{path}" failed to import.').format(path=script_file_path),
                _failure_detail(script_file_path, error, revision_dir),
            ) from error


@contextmanager
def revision_import_session(storage_key, digest):
    """
    Hold one revision's lock across a whole multi-step session.

    A validation imports several script files, inspects the results, and unloads at the
    end. Two revision rows can share one source digest and therefore one namespace, so
    without the session a concurrent holder could unload between a sibling's steps. Inner
    import and unload calls nest through the lock's reentrancy.
    """
    with _revision_lock(storage_key, digest):
        yield


def unload_revision(storage_key, digest):
    """
    Remove one revision's modules from the process and return the removed names.

    Only sys.modules entries below the revision namespace and the container binding go
    away. The cache tree stays on disk, nothing outside the private namespace is ever
    touched, and a revision that was never imported unloads to an empty tuple.
    """
    revision_name = revision_module_name(storage_key, digest)
    with _revision_lock(storage_key, digest):
        removed = sorted(_loaded_names(revision_name))
        for name in removed:
            del sys.modules[name]
        project_module = sys.modules.get(project_module_name(storage_key))
        attribute = revision_name.rsplit('.', 1)[1]
        if project_module is not None and hasattr(project_module, attribute):
            delattr(project_module, attribute)
        return tuple(removed)


def _revision_lock(storage_key, digest):
    """
    Return the process lock serializing imports and unloads of one revision namespace.

    Entries are never dropped. Evicting one would hand two holders separate locks for one
    namespace, which is the race the registry exists to prevent, and a lock per revision
    ever loaded costs next to nothing.
    """
    with _revision_locks_guard:
        return _revision_locks.setdefault(revision_module_name(storage_key, digest), threading.RLock())


def _loaded_names(revision_name):
    """Return every sys.modules name inside one revision's namespace."""
    prefix = f'{revision_name}.'
    return {name for name in sys.modules if name == revision_name or name.startswith(prefix)}


def _register_container(name, parent=None):
    """Return the synthetic container package registered under one name, creating it once."""
    module = sys.modules.get(name)
    if module is None:
        spec = importlib.machinery.ModuleSpec(name, None, is_package=True)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
    if parent is not None:
        setattr(parent, name.rsplit('.', 1)[1], module)
    return module


def _ensure_parent_packages(storage_key):
    """
    Register the namespace root and one project's container, returning the container name.

    Containers hold no project code, so they sit outside the failure boundary and survive
    any revision's failed import.
    """
    project_name = project_module_name(storage_key)
    with _namespace_lock:
        root = _register_container(PRIVATE_ROOT)
        _register_container(project_name, parent=root)
    return project_name


def _register_revision_package(revision_name, revision_dir, has_root_init, project_module):
    """
    Register one revision's package, executing its root __init__ when the tree ships one.

    The module lands in sys.modules and on its container before the root module body
    runs, exactly like a regular package import, so circular imports inside the revision
    behave normally. The caller wraps this in the failure boundary because the root body
    is project code.
    """
    if has_root_init:
        spec = importlib.util.spec_from_file_location(
            revision_name,
            revision_dir / '__init__.py',
            submodule_search_locations=[str(revision_dir)],
        )
        # Registered by hand rather than resolved through the finder, so it wraps explicitly.
        spec.loader = wrap_loader(spec.loader)
    else:
        spec = importlib.machinery.ModuleSpec(revision_name, None, is_package=True)
        spec.submodule_search_locations = [str(revision_dir)]
    module = importlib.util.module_from_spec(spec)
    sys.modules[revision_name] = module
    setattr(project_module, revision_name.rsplit('.', 1)[1], module)
    if has_root_init:
        spec.loader.exec_module(module)


def _sweep_added(revision_name, before, project_module):
    """
    Remove every module a failed import added below one revision namespace.

    The container binding goes too when the revision package itself was new. Names loaded
    before the boundary opened stay, a failed sibling script file must not tear down what
    an earlier import legitimately published.
    """
    for name in _loaded_names(revision_name) - before:
        del sys.modules[name]
    attribute = revision_name.rsplit('.', 1)[1]
    if revision_name not in before and project_module is not None and hasattr(project_module, attribute):
        delattr(project_module, attribute)


def _failure_detail(script_file_path, error, revision_dir):
    """
    Build the structured record describing one failed script file import.

    The traceback keeps only frames inside the revision tree when any exist, so the
    record centers on project code rather than import machinery. Raw runtime paths may
    remain in the text, storing user-facing copies is the validation layer's job and it
    sanitizes first.
    """
    frames = traceback.extract_tb(error.__traceback__)
    local = [frame for frame in frames if frame.filename.startswith(str(revision_dir))]
    return {
        'path': script_file_path,
        'code': 'script_file_import_failed',
        'message': str(error),
        'exception_type': type(error).__name__,
        'traceback': ''.join(traceback.format_list(local or frames)),
    }
