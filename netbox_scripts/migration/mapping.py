"""Which Script one built-in Custom Script becomes."""

from django.core.exceptions import ValidationError

from ..models import NetBoxScript
from ..utils import source_path_to_dotted_name
from . import plan
from . import source as legacy_source

__all__ = (
    'build_map',
    'project_keys',
    'recorded',
    'resolve_scripts',
)


def build_map(modules=None, *, resolve_existing=None):
    """
    Return the plugin identity every built-in Custom Script would migrate to.

    Live mapping resolves existing Projects by the same identity as staging. Supplied modules
    remain a pure preview unless resolve_existing=True is requested. Unimportable module paths
    are reported under 'unmapped'.
    """
    if resolve_existing is None:
        resolve_existing = modules is None
    modules = legacy_source.legacy_modules() if modules is None else modules
    proposals = plan.group(modules)
    keys = {}
    for proposal in proposals:
        existing = plan.existing_project(proposal) if resolve_existing else None
        keys[proposal.key] = existing.key if existing is not None else proposal.key
    proposed = {pk: project_plan for project_plan in proposals for pk in project_plan.module_pks}
    mapped, scripts, unmapped = [], [], []
    for module in modules:
        project_plan = proposed[module.pk]
        try:
            source_path = project_plan.source_path_for(module)
            if source_path is None:
                raise ValidationError('The module sits outside the proposed project directory.')
            # The dotted name, not the source path: a NetBoxScript records the module a class was
            # defined in, which discovery reads off cls.__module__.
            module_path = source_path_to_dotted_name(source_path)
        except ValidationError as error:
            unmapped.append(
                {'legacy_pk': module.pk, 'path': module.data_path or module.file_path, 'reason': error.messages[0]}
            )
            continue
        mapped.append({'legacy_pk': module.pk, 'project_key': keys[project_plan.key], 'source_path': source_path})
        scripts.extend(
            {
                'legacy_pk': script.pk,
                'legacy_module_pk': module.pk,
                'legacy_name': script.name,
                'project_key': keys[project_plan.key],
                'module_path': module_path,
                # The built-in feature records a Script by its Python class name, so the plugin's
                # class_name is the same string under a different column.
                'class_name': script.name,
            }
            # Only what still publishes: a soft-deleted class has no counterpart and never will.
            for script in module.scripts
            if script.is_executable
        )
    return {'modules': mapped, 'scripts': scripts, 'unmapped': unmapped}


def recorded(run):
    """Return the map the cutover froze on the run, or None before the fence."""
    return run.journal.get('mapping') if run else None


def project_keys(mapping):
    """Return the keys of every Project this migration covers, in order."""
    return sorted({entry['project_key'] for entry in mapping['modules']})


def resolve_scripts(mapping):
    """
    Return the row each mapped Script resolves to, keyed by legacy key, and the entries that do not.

    One query per project rather than one per script. A retired row still resolves: this answers
    which row an identity names, not whether it can run. An entry resolving to nothing is returned
    rather than raised.
    """
    grouped = {}
    for entry in mapping['scripts']:
        grouped.setdefault(entry['project_key'], []).append(entry)
    resolved, unresolved = {}, []
    for key, entries in grouped.items():
        rows = {
            (row.module_path, row.class_name): row
            for row in NetBoxScript.objects.filter(project__key=key).select_related('project')
        }
        for entry in entries:
            row = rows.get((entry['module_path'], entry['class_name']))
            if row is None:
                unresolved.append(entry)
            else:
                resolved[entry['legacy_pk']] = row
    return resolved, unresolved
