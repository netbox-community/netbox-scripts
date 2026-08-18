"""The last step: deleting the built-in rows once nothing an installation holds refers to them."""

from django.utils.translation import gettext_lazy as _

from ..choices import MigrationStateChoices
from ..models import CustomScriptProject
from . import cutover, mapping
from . import references as legacy_references
from . import source as legacy_source

__all__ = (
    'STEP',
    'retire_legacy',
)

STEP = 'cleanup'


def retire_legacy(run):
    """
    Delete the built-in modules this migration replaced, skipping any whose deletion would lose data.

    Returns the counts and the warnings naming what was left behind, and returns the recorded counts
    unchanged without touching anything once the step has completed. The state reaches migrated only
    when nothing was left behind, so a partial pass stays resumable. Raises CutoverRefused unless the
    reference pass has moved the Job history off the built-in rows.
    """
    if run is None:
        raise cutover.CutoverRefused(_('No migration is open, so there is nothing left to retire.'))
    if run.step_done(STEP):
        return run.recorded_counts(STEP), []
    cutover.require_staged(run)
    if not run.step_done(legacy_references.HISTORY_STEP):
        raise cutover.CutoverRefused(
            _('The Job history has not been repointed yet, and deleting a Script would take its jobs with it.')
        )

    counts, warnings = _delete_modules(run)
    counts.update(legacy_source.reference_counts())
    if counts['skipped'] or counts['unserved']:
        # Left open on purpose: the operator clears what each warning names and runs this again.
        return counts, warnings
    run.advance(MigrationStateChoices.MIGRATED)
    run.complete_step(STEP, counts, warnings)
    return counts, warnings


def _delete_modules(run):
    """Delete each of this migration's modules that nothing refers to, one row at a time."""
    plugin_map = mapping.build_map()
    migrated = _migrated_project_keys(run)
    serving = set(
        CustomScriptProject.objects.filter(key__in=migrated, active_revision__isnull=False).values_list(
            'key', flat=True
        )
    )
    # build_map() maps whatever the built-in feature holds right now, so a module added after this
    # migration staged would otherwise be deleted by it. The activation step's journal is what says
    # which Projects are this migration's, and a module outside them is nobody's business here.
    mine = [entry for entry in plugin_map['modules'] if entry['project_key'] in migrated]
    keys = [entry['legacy_pk'] for entry in mine if entry['project_key'] in serving]
    counts = {'modules': 0, 'scripts': 0, 'skipped': 0, 'unserved': len(mine) - len(keys)}
    warnings = []
    if counts['unserved']:
        waiting = sorted({entry['project_key'] for entry in mine if entry['project_key'] not in serving})
        warnings.append(
            _(
                'Built-in modules were left in place because these Projects are not serving a revision: '
                '{keys}. Activate them, then run this again.'
            ).format(keys=', '.join(waiting))
        )
    # Instance by instance, never through the queryset: QuerySet.delete() does not call the model's
    # delete(), which is what removes the stored source file, and iterating is what keeps the
    # per-module refusal below meaningful.
    for module in legacy_source.legacy_modules_by_pk(keys):
        references = legacy_source.module_references(module)
        if held := _refusal_for(module, references):
            counts['skipped'] += 1
            warnings.append(held)
            continue
        counts['scripts'] += references['scripts']
        counts['modules'] += 1
        module.delete()
    return counts, warnings


def _migrated_project_keys(run):
    """Return the keys of the Projects this migration activated, as its journal recorded them."""
    recorded = run.journal.get('steps', {}).get(cutover.ACTIVATE_STEP, {}).get('projects') or []
    return {entry['project_key'] for entry in recorded if entry.get('project_key')}


def _refusal_for(module, references):
    """Return why one module cannot be deleted yet, or None when nothing refers to it any longer."""
    # Each of these would be deleted with the module rather than orphaned, so a soft delete on the
    # Script protects none of them: the module delete that follows takes it anyway.
    if references['module_jobs']:
        return _(
            'Built-in script module {name} holds Job history of its own that no Custom Script Project can '
            'hold, so it was left in place. Delete those Jobs to retire it.'
        ).format(name=module.python_name)
    if references['script_jobs']:
        return _(
            'Built-in script module {name} has a Script that still holds Job history, so it was left in '
            'place rather than deleted with that history. Repoint or delete those Jobs to retire it.'
        ).format(name=module.python_name)
    if references['event_rules']:
        return _(
            'Built-in script module {name} is still named by an Event Rule, which would be deleted with it, '
            'so it was left in place. Repoint or delete that rule to retire it.'
        ).format(name=module.python_name)
    return None
