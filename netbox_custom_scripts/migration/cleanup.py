"""The last step: deleting the built-in rows once nothing an installation holds refers to them."""

from django.utils.translation import gettext_lazy as _

from ..choices import MigrationStateChoices
from ..models import CustomScriptProject
from . import cutover, mapping
from . import references as legacy_references
from . import source as legacy_source

__all__ = (
    'STEP',
    'ready',
    'retire_legacy',
)

STEP = 'cleanup'

# A schedule is recreated through the built-in rows this pass deletes, not just its Job history.
_REQUIRED_STEPS = (
    legacy_references.EVENT_RULES_STEP,
    legacy_references.PERMISSIONS_STEP,
    legacy_references.HISTORY_STEP,
    legacy_references.SCHEDULES_STEP,
)


def ready(run):
    """Whether every step this pass depends on has completed, which is what the page offers it on."""
    return all(run.step_done(name) for name in _REQUIRED_STEPS)


def retire_legacy(run):
    """
    Delete the built-in modules this migration replaced, skipping any whose deletion would lose data.

    Returns the counts and the warnings naming what was left behind, and returns the recorded counts
    unchanged without touching anything once the step has completed. A refusal is either retained,
    which the state ignores, or blocked, which leaves the run open. Raises CutoverRefused unless
    every reference step has completed.
    """
    if run is None:
        raise cutover.CutoverRefused(_('No migration is open, so there is nothing left to retire.'))
    if run.step_done(STEP):
        return run.recorded_counts(STEP), []
    cutover.require_staged(run)
    if not ready(run):
        outstanding = [name for name in _REQUIRED_STEPS if not run.step_done(name)]
        raise cutover.CutoverRefused(
            _(
                'The reference pass has not finished: {steps} still outstanding. Deleting a Script would take '
                'its Job history with it, and a schedule can only be recreated while the built-in rows are '
                'still here.'
            ).format(steps=', '.join(outstanding))
        )

    counts, warnings = _delete_modules(run)
    counts.update(legacy_source.reference_counts())
    if counts['blocked'] or counts['unserved']:
        # Left open on purpose: the operator clears what each warning names and runs this again.
        return counts, warnings
    # Retained does not hold the run open, because nothing an operator does would ever clear it.
    run.advance(MigrationStateChoices.MIGRATED)
    run.complete_step(STEP, counts, warnings)
    return counts, warnings


def _delete_modules(run):
    """Delete each of this migration's modules that nothing refers to, one row at a time."""
    plugin_map = mapping.recorded(run)
    mine = plugin_map['modules']
    serving = set(
        CustomScriptProject.objects.filter(
            key__in=mapping.project_keys(plugin_map), active_revision__isnull=False
        ).values_list('key', flat=True)
    )
    keys = [entry['legacy_pk'] for entry in mine if entry['project_key'] in serving]
    counts = {'modules': 0, 'scripts': 0, 'blocked': 0, 'retained': 0, 'unserved': len(mine) - len(keys)}
    warnings = []
    if counts['unserved']:
        waiting = sorted({entry['project_key'] for entry in mine if entry['project_key'] not in serving})
        warnings.append(
            _(
                'Built-in modules were left in place because these Projects are not serving a revision: '
                '{keys}. Activate them, then run this again.'
            ).format(keys=', '.join(waiting))
        )
    _resolved, unresolved = mapping.resolve_scripts(plugin_map)
    stranded = {entry['legacy_module_pk'] for entry in unresolved}
    # Instance by instance: QuerySet.delete() skips the delete() that removes the stored file.
    for module in legacy_source.legacy_modules_by_pk(keys):
        references = legacy_source.module_references(module)
        permanent, held = _refusal_for(module, references, module.pk in stranded)
        if held:
            counts['retained' if permanent else 'blocked'] += 1
            warnings.append(held)
            continue
        counts['scripts'] += references['scripts']
        counts['modules'] += 1
        module.delete()
    return counts, warnings


def _refusal_for(module, references, stranded):
    """Return whether a refusal is permanent and why one module cannot be deleted, or (False, None)."""
    name = module.python_name
    # Permanent means no operator action clears it, so the run closes with the module in place.
    if references['module_jobs']:
        return True, _(
            'Built-in script module {name} holds Job history of its own, which no Custom Script Project '
            'can hold, so it stays where it is.'
        ).format(name=name)
    if references['retired_script_jobs']:
        return True, _(
            'Built-in script module {name} holds Job history for a class that left the file, which no '
            'Custom Script replaces, so it stays where it is.'
        ).format(name=name)
    if references['live_script_jobs']:
        return False, _(
            'Built-in script module {name} has a Script that still holds Job history the reference pass '
            'did not move. Run that pass again, then retry this one.'
        ).format(name=name)
    if references['event_rules']:
        return False, _(
            'Built-in script module {name} is still named by an Event Rule the reference pass did not '
            'move. Run that pass again, then retry this one.'
        ).format(name=name)
    if stranded:
        return False, _(
            'Built-in script module {name} publishes a class no Custom Script resolves to, so deleting it '
            'would leave that script unable to run at all. Fix the source and stage it again.'
        ).format(name=name)
    return False, None
