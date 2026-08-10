"""
Script discovery for imported entrypoint modules.

An entrypoint publishes the Script subclasses its own body defines. Classes living in
other revision modules publish only through an explicit script_order listing, which also
fixes presentation order, and classes imported from installed packages never publish, so
what a revision offers is always spelled out in the revision itself. A class declaring
tests and no run() is a legacy report rather than a script, and is refused here instead of
published, because publication is what would offer an operator a script that cannot run.
Each published class
receives identity markers in its own class dictionary: the logical module path a user
recognizes and a project-qualified logger name, so runtime namespaces never leak into
user-facing identity and two projects always log apart.
"""

from typing import NamedTuple

from ..scripts.base import BaseScript, Script
from .exceptions import DiscoveryError

__all__ = (
    'DiscoveredScript',
    'discover_scripts',
)

LOGGER_PREFIX = 'netbox.plugins.netbox_custom_scripts.scripts'


class DiscoveredScript(NamedTuple):
    """One publishable script class with its user-facing identity."""

    cls: type
    logical_module: str
    name: str


def discover_scripts(module, *, project_key, revision_prefix):
    """
    Return the scripts one imported entrypoint publishes, in presentation order.

    script_order entries come first in their listed order, every other Script subclass
    the module body defines follows alphabetically by bound name, and one class publishes
    once however many names it is bound to. Raises DiscoveryError when script_order
    breaks the publication contract or two published classes collide on identity.
    """
    published = list(_order_entries(module, revision_prefix))
    seen = set(published)
    for _bound_name, candidate in sorted(vars(module).items()):
        if not _defined_here(module, candidate) or candidate in seen:
            continue
        seen.add(candidate)
        published.append(candidate)
    results = [_publish(cls, project_key, revision_prefix) for cls in published]
    _require_distinct_identities(results)
    return results


def _order_entries(module, revision_prefix):
    """Yield the validated script_order entries of one module, in listed order."""
    order = getattr(module, 'script_order', ())
    if not isinstance(order, (list, tuple)):
        raise DiscoveryError('script_order must be a list or tuple of script classes.', code='invalid_script_order')
    seen = set()
    for entry in order:
        if not isinstance(entry, type) or not issubclass(entry, Script):
            raise DiscoveryError(
                'Every script_order entry must be a Script subclass.',
                code='not_a_script',
                name=getattr(entry, '__name__', None),
            )
        if not entry.__module__.startswith(f'{revision_prefix}.'):
            raise DiscoveryError(
                f'"{entry.__name__}" is not defined in a module of this revision. Classes from installed '
                f'packages or the package root cannot be published.',
                code='not_revision_local',
                name=entry.__name__,
            )
        if entry in seen:
            raise DiscoveryError(
                f'"{entry.__name__}" appears more than once in script_order.',
                code='duplicate_entry',
                name=entry.__name__,
            )
        seen.add(entry)
        yield entry


def _defined_here(module, candidate):
    """Return whether a module member is a Script subclass the module body itself defines."""
    return isinstance(candidate, type) and issubclass(candidate, Script) and candidate.__module__ == module.__name__


def _report_style(cls):
    """Return whether a class is a legacy report rather than a runnable script."""
    if getattr(cls, '_custom_script_report', False):
        return True
    if cls.run is not BaseScript.run:
        return False
    # Only a callable counts: an attribute named test_mode is data, and a script declaring one
    # has to stay publishable.
    return any(name.startswith('test_') and callable(getattr(cls, name, None)) for name in dir(cls))


def _publish(cls, project_key, revision_prefix):
    """Stamp one class with its identity markers and describe the publication."""
    if _report_style(cls):
        raise DiscoveryError(
            f'"{cls.__name__}" is a report rather than a Custom Script. Reports are not supported. '
            f'Give the class a run(self, data, commit) method to publish it as a script.',
            code='report_style',
            name=cls.__name__,
        )
    logical_module = cls.__module__[len(revision_prefix) + 1 :]
    cls._custom_script_module = logical_module
    cls._custom_script_logger_name = f'{LOGGER_PREFIX}.{project_key}.{logical_module}.{cls.__name__}'
    return DiscoveredScript(cls=cls, logical_module=logical_module, name=cls.__name__)


def _require_distinct_identities(results):
    """Refuse a publication set where two classes share one logical identity."""
    seen = set()
    for result in results:
        identity = (result.logical_module, result.name)
        if identity in seen:
            raise DiscoveryError(
                f'Two script classes publish as "{result.logical_module}.{result.name}".',
                code='duplicate_identity',
                name=result.name,
            )
        seen.add(identity)
