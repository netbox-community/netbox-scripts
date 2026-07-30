"""
Recovery of a live script class from a stored identity.

Validation records what a revision publishes. This module walks that record back: given the
project-qualified identity a Custom Script row holds, it finds the entrypoint that surfaced
the class, imports it, and returns the class itself.

Going through the recorded entrypoint is what makes the walk possible at all. A Custom Script
is identified by the module that defines it, and script_order lets a class defined in a helper
publish through an entrypoint elsewhere, so the defining module is often not importable as an
entrypoint and has no declaration of its own. The provenance the snapshot carries is the only
route from one to the other.

Nothing here opens an import session or unloads afterwards. The caller runs the class it gets
back, so the namespace has to outlive this call, and the session is the caller's to hold.
"""

from .discovery import discover_scripts
from .exceptions import ScriptResolutionError
from .introspection import validate_discovered_scripts
from .loader import import_entrypoint
from .naming import revision_module_name

__all__ = ('resolve_script_class',)


def resolve_script_class(
    storage_key,
    digest,
    *,
    discovered_scripts,
    project_key,
    module_path,
    class_name,
    storage,
    manifest,
    passthrough=(),
    cache_root=None,
):
    """
    Return the class one stored identity names, importing the entrypoint that publishes it.

    The discovery snapshot is validated before it is read, so a record changed outside
    validation is refused rather than followed. passthrough lists exception types that must
    escape the import unwrapped. Raises ScriptResolutionError when the snapshot does not name
    the identity or the entrypoint no longer publishes it, ScriptMetadataError for a snapshot
    a build could not have produced, and whatever the import raises for a revision that cannot
    be imported.
    """
    identity = f'{module_path}.{class_name}'
    record = _find_record(validate_discovered_scripts(discovered_scripts), module_path, class_name)
    if record is None:
        raise ScriptResolutionError(
            f'This revision does not publish "{identity}".',
            code='not_published',
            name=identity,
        )

    module = import_entrypoint(
        storage_key,
        digest,
        record['entrypoint_path'],
        storage=storage,
        manifest=manifest,
        passthrough=passthrough,
        cache_root=cache_root,
    )
    revision_prefix = revision_module_name(storage_key, digest)
    for found in discover_scripts(module, project_key=project_key, revision_prefix=revision_prefix):
        if (found.logical_module, found.name) == (module_path, class_name):
            return found.cls

    raise ScriptResolutionError(
        f'"{record["entrypoint_path"]}" no longer publishes "{identity}".',
        code='no_longer_published',
        name=identity,
    )


def _find_record(discovered_scripts, module_path, class_name):
    """Return the snapshot record for one identity, or None when the snapshot has no such entry."""
    for record in discovered_scripts:
        if (record['module_path'], record['class_name']) == (module_path, class_name):
            return record
    return None
