"""
Recovery of a live script class from a stored identity.

Validation records what a revision publishes. This module walks that record back: given the
project-qualified identity a Script row holds, it finds the script file that surfaced
the class, imports it, and returns the class itself.

Going through the recorded script file is what makes the walk possible at all. A Script
is identified by the module that defines it, and script_order lets a class defined in a helper
publish through a script file elsewhere, so the defining module is often not importable as a
script file and has no declaration of its own. The provenance the snapshot carries is the only
route from one to the other.

Nothing here opens an import session or unloads afterwards. The caller runs the class it gets
back, so the namespace has to outlive this call, and the session is the caller's to hold.
"""

from django.utils.translation import gettext_lazy as _

from .discovery import discover_scripts
from .exceptions import ScriptResolutionError
from .introspection import validate_discovered_scripts
from .loader import import_script_file
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
    Return the class one stored identity names, importing the script file that publishes it.

    The discovery snapshot is validated before it is read, so a record changed outside
    validation is refused rather than followed. passthrough lists exception types that must
    escape the import unwrapped. Raises ScriptResolutionError when the snapshot does not name
    the identity or the script file no longer publishes it, ScriptMetadataError for a snapshot
    a build could not have produced, and whatever the import raises for a revision that cannot
    be imported.
    """
    identity = f'{module_path}.{class_name}'
    record = next(
        (
            entry
            for entry in validate_discovered_scripts(discovered_scripts)
            if (entry['module_path'], entry['class_name']) == (module_path, class_name)
        ),
        None,
    )
    if record is None:
        raise ScriptResolutionError(
            _('This revision does not publish "{identity}".').format(identity=identity),
            code='not_published',
            name=identity,
        )

    module = import_script_file(
        storage_key,
        digest,
        record['script_file_path'],
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
        _('"{path}" no longer publishes "{identity}".').format(path=record['script_file_path'], identity=identity),
        code='no_longer_published',
        name=identity,
    )
