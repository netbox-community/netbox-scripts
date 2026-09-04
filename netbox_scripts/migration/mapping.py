"""Which plugin Custom Script one built-in Script becomes."""

from django.core.exceptions import ValidationError

from ..choices import ProjectSourceTypeChoices
from ..ingestion import uploaded_source_path
from ..models import CustomScript
from ..utils import data_source_relative_path, source_path_to_dotted_name
from . import plan
from . import source as legacy_source

__all__ = (
    'build_map',
    'project_keys',
    'recorded',
    'resolve_scripts',
)


def build_map(modules=None):
    """
    Return the plugin identity every built-in Script would migrate to, reading no plugin rows.

    Pass modules to map supplied data rather than the live installation, as build_report does.
    Grouping comes from plan.group(), so a project key here is the key staging creates, and the
    identity is derived rather than stored. A module whose path could never be imported is
    reported under 'unmapped' rather than raising.
    """
    modules = legacy_source.legacy_modules() if modules is None else modules
    proposed = {pk: project_plan for project_plan in plan.group(modules) for pk in project_plan.module_pks}
    mapped, scripts, unmapped = [], [], []
    for module in modules:
        project_plan = proposed[module.pk]
        try:
            source_path = _source_path(module, project_plan)
            if source_path is None:
                raise ValidationError('The module sits outside the proposed project directory.')
            # The dotted name, not the source path: a CustomScript records the module a class was
            # defined in, which discovery reads off cls.__module__.
            module_path = source_path_to_dotted_name(source_path)
        except ValidationError as error:
            unmapped.append(_unmapped(module, error.messages[0]))
            continue
        mapped.append({'legacy_pk': module.pk, 'project_key': project_plan.key, 'source_path': source_path})
        scripts.extend(
            {
                'legacy_pk': script.pk,
                'legacy_module_pk': module.pk,
                'legacy_name': script.name,
                'project_key': project_plan.key,
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
            for row in CustomScript.objects.filter(project__key=key).select_related('project')
        }
        for entry in entries:
            row = rows.get((entry['module_path'], entry['class_name']))
            if row is None:
                unresolved.append(entry)
            else:
                resolved[entry['legacy_pk']] = row
    return resolved, unresolved


def _source_path(module, project_plan):
    """Return the project-relative path a legacy module's content is staged at."""
    # Both branches mirror staging exactly, because the identity has to match what it created.
    if project_plan.source_type == ProjectSourceTypeChoices.UPLOAD:
        return uploaded_source_path(module.file_path)
    return data_source_relative_path(module.data_path, project_plan.data_path)


def _unmapped(module, reason):
    """Return one report row for a module no plugin identity can be derived for."""
    return {'legacy_pk': module.pk, 'path': module.data_path or module.file_path, 'reason': reason}
