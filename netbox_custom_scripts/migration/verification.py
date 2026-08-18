"""Reporting whether a migration landed, by reading only."""

from django.utils.translation import gettext_lazy as _

from ..models import CustomScript, CustomScriptProject, MigrationRun
from . import cleanup, cutover, mapping, plan
from . import references as legacy_references
from . import source as legacy_source

__all__ = ('verify',)

# Which check each level answers for, so a caller can act on one without parsing its message.
MODULES = 'modules'
SCRIPTS = 'scripts'
EVENT_RULES = 'event_rules'
PERMISSIONS = 'permissions'
JOBS = 'jobs'


def verify(run=None):
    """
    Report whether a migration landed, changing nothing and refusing nothing.

    Reads the latest migration rather than the open one, so a finished run still reports. Safe at any
    state, including before a cutover and after the built-in rows are gone. Every check names which
    side it read.
    """
    run = run or MigrationRun.objects.first()
    if run is None:
        return _report(
            [_check(MODULES, plan.WARNING, _('No migration has been started, so there is nothing to verify.'))]
        )
    live_modules = legacy_source.legacy_modules()
    return _report(
        [
            _verify_modules(run, live_modules),
            _verify_scripts(run, live_modules),
            _verify_event_rules(run),
            _verify_permissions(run),
            _verify_jobs(run),
        ]
    )


def _report(checks):
    """Return the report, taking the worst level any check carries as its status."""
    levels = {check['level'] for check in checks}
    status = plan.BLOCKING if plan.BLOCKING in levels else plan.WARNING if plan.WARNING in levels else plan.READY
    return {'status': status, 'checks': checks}


def _check(name, level, message, source=None):
    """Return one row of the report."""
    return {'name': name, 'level': level, 'message': message, 'source': source or _('the migration journal')}


def _activated_keys(run):
    """Return the Project keys the activation step recorded, which is what this migration produced."""
    recorded = run.journal.get('steps', {}).get(cutover.ACTIVATE_STEP, {}).get('projects') or []
    return sorted({entry['project_key'] for entry in recorded if entry.get('project_key')})


def _verify_modules(run, live_modules):
    """Every Project this migration produced exists and serves a revision."""
    if not run.step_done(cutover.ACTIVATE_STEP):
        return _check(MODULES, plan.WARNING, _('The staged Projects have not been activated yet.'))
    keys = _activated_keys(run)
    serving = set(
        CustomScriptProject.objects.filter(key__in=keys, active_revision__isnull=False).values_list('key', flat=True)
    )
    if missing := [key for key in keys if key not in serving]:
        return _check(
            MODULES,
            plan.BLOCKING,
            _('{count} of {total} migrated Project(s) serve no revision: {keys}.').format(
                count=len(missing), total=len(keys), keys=', '.join(missing)
            ),
        )
    remaining = len(live_modules)
    if remaining and not run.step_done(cleanup.STEP):
        return _check(
            MODULES,
            plan.WARNING,
            _(
                'Every one of the {total} migrated Project(s) serves a revision, and {count} built-in module(s) '
                'have not been retired yet.'
            ).format(total=len(keys), count=remaining),
            source=_('the journal and the built-in rows'),
        )
    return _check(
        MODULES,
        plan.READY,
        _('All {total} migrated Project(s) serve a revision.').format(total=len(keys)),
    )


def _verify_scripts(run, live_modules):
    """Every built-in Script has a live Custom Script, read from whichever side still holds them."""
    if not run.step_done(cutover.ACTIVATE_STEP):
        return _check(SCRIPTS, plan.WARNING, _('No Custom Script exists yet, because nothing has been activated.'))
    if live_modules:
        # The frozen map where there is one, so a partial cleanup cannot move the comparison.
        plugin_map = mapping.recorded(run) or mapping.build_map(modules=live_modules)
        resolved, unresolved = mapping.resolve_scripts(plugin_map)
        if unresolved:
            return _check(
                SCRIPTS,
                plan.BLOCKING,
                _('{count} built-in Script(s) resolve to no Custom Script: {names}.').format(
                    count=len(unresolved), names=', '.join(sorted(entry['legacy_name'] for entry in unresolved))
                ),
                source=_('the built-in rows'),
            )
        if retired := sorted(row.class_name for row in resolved.values() if row.is_retired):
            return _check(
                SCRIPTS,
                plan.BLOCKING,
                _('{count} Custom Script(s) that replace a built-in one are retired: {names}.').format(
                    count=len(retired), names=', '.join(retired)
                ),
                source=_('the built-in rows'),
            )
        return _check(
            SCRIPTS,
            plan.READY,
            _('All {count} built-in Script(s) resolve to a Custom Script that can run.').format(count=len(resolved)),
            source=_('the built-in rows'),
        )
    # The built-in rows are gone, so what a Project publishes is the only remaining evidence.
    keys = _activated_keys(run)
    published = CustomScript.objects.filter(project__key__in=keys)
    if barren := sorted(set(keys) - set(published.values_list('project__key', flat=True))):
        return _check(
            SCRIPTS,
            plan.BLOCKING,
            _('The built-in rows are gone and {count} migrated Project(s) publish no Custom Script: {keys}.').format(
                count=len(barren), keys=', '.join(barren)
            ),
        )
    if retired := published.filter(is_retired=True).count():
        return _check(
            SCRIPTS,
            plan.WARNING,
            _(
                'The built-in rows are gone. {count} of the {total} Custom Script(s) the migrated Projects '
                'publish are retired.'
            ).format(count=retired, total=published.count()),
        )
    return _check(
        SCRIPTS,
        plan.READY,
        _(
            'The built-in rows are gone and the migrated Projects publish {count} Custom Script(s), none retired.'
        ).format(count=published.count()),
    )


def _verify_event_rules(run):
    """No Event Rule names the built-in feature, and every one that was enabled is enabled again."""
    captured = run.journal.get('event_rules') or []
    if not run.step_done(legacy_references.EVENT_RULES_STEP):
        return _check(
            EVENT_RULES,
            plan.WARNING,
            _('The {count} captured Event Rule(s) have not been repointed yet.').format(count=len(captured)),
        )
    # A rule the repoint could not move is deliberate and permanent until an operator acts, while one
    # this migration never captured was created after the cutover. Only the second is a fault, and
    # the journal is the only thing that tells them apart.
    known = {entry['pk']: entry['name'] for entry in captured}
    stranded = list(legacy_source.legacy_event_rules())
    if appeared := sorted(str(rule) for rule in stranded if rule.pk not in known):
        return _check(
            EVENT_RULES,
            plan.BLOCKING,
            _(
                '{count} Event Rule(s) name the built-in feature and this migration never captured them: {names}.'
            ).format(count=len(appeared), names=', '.join(appeared)),
            source=_('the journal and the built-in rows'),
        )
    if left := sorted(known[rule.pk] for rule in stranded):
        return _check(
            EVENT_RULES,
            plan.WARNING,
            _('{count} Event Rule(s) could not be moved off the built-in feature and stay withdrawn: {names}.').format(
                count=len(left), names=', '.join(left)
            ),
            source=_('the journal and the built-in rows'),
        )
    if withdrawn := _still_withdrawn(captured):
        return _check(
            EVENT_RULES,
            plan.WARNING,
            _(
                'No Event Rule names the built-in feature. {count} that were enabled before the cutover are '
                'still disabled: {names}.'
            ).format(count=len(withdrawn), names=', '.join(withdrawn)),
            source=_('the journal and the live rules'),
        )
    return _check(
        EVENT_RULES,
        plan.READY,
        _(
            'None of the {count} captured Event Rule(s) name the built-in feature, and each one that was '
            'enabled is enabled again.'
        ).format(count=len(captured)),
    )


def _still_withdrawn(captured):
    """Return the names of rules that were enabled before the cutover and are not now."""
    from extras.models import EventRule

    wanted = {entry['pk'] for entry in captured if entry['enabled']}
    if not wanted:
        return []
    live = dict(EventRule.objects.filter(pk__in=wanted).values_list('pk', 'enabled'))
    return sorted(entry['name'] for entry in captured if entry['pk'] in wanted and not live.get(entry['pk'], True))


def _verify_permissions(run):
    """No permission names the built-in feature, and the ones left by hand are named again."""
    captured = run.journal.get('permissions') or []
    if not run.step_done(legacy_references.PERMISSIONS_STEP):
        return _check(
            PERMISSIONS,
            plan.WARNING,
            _('The {count} captured permission(s) have not been repointed yet.').format(count=len(captured)),
        )
    # Same split as the Event Rules check: a permission carrying constraints is left withdrawn and
    # untranslated on purpose, so it still names the built-in feature and always will until somebody
    # recreates it. One this migration never captured was granted after the cutover.
    known = {entry['pk']: entry['name'] for entry in captured}
    stranded = list(legacy_source.legacy_permissions())
    if appeared := sorted(permission.name for permission in stranded if permission.pk not in known):
        return _check(
            PERMISSIONS,
            plan.BLOCKING,
            _(
                '{count} permission(s) name the built-in feature and this migration never captured them: {names}.'
            ).format(count=len(appeared), names=', '.join(appeared)),
            source=_('the journal and the built-in rows'),
        )
    # Restated on every run rather than once in a job log, because recreating one is operator work
    # that stays outstanding until somebody does it.
    if left := sorted(known[permission.pk] for permission in stranded):
        return _check(
            PERMISSIONS,
            plan.WARNING,
            _(
                '{count} permission(s) could not be moved onto the plugin and were left withdrawn for you to '
                'recreate: {names}.'
            ).format(count=len(left), names=', '.join(left)),
            source=_('the journal and the built-in rows'),
        )
    return _check(
        PERMISSIONS,
        plan.READY,
        _('None of the {count} captured permission(s) name the built-in feature.').format(count=len(captured)),
    )


def _verify_jobs(run):
    """No Job names the built-in feature, and every captured schedule has a live counterpart."""
    captured = run.journal.get('schedules') or []
    if not run.step_done(legacy_references.HISTORY_STEP):
        return _check(JOBS, plan.WARNING, _('The Job history has not been repointed yet.'))
    recreated = run.journal.get('recreated_schedules') or {}
    missing = [entry for entry in captured if str(entry['job_pk']) not in recreated]
    counts = legacy_source.reference_counts()
    if counts['jobs']:
        return _check(
            JOBS,
            plan.WARNING,
            _(
                '{count} Job(s) still name the built-in feature. Each one is history no Custom Script Project '
                'can hold, so it stays where it is.'
            ).format(count=counts['jobs']),
            source=_('the built-in rows'),
        )
    if missing:
        return _check(
            JOBS,
            plan.WARNING,
            _(
                'No Job names the built-in feature. {count} of the {total} captured schedule(s) were not '
                'recreated: {names}.'
            ).format(
                count=len(missing), total=len(captured), names=', '.join(sorted(entry['name'] for entry in missing))
            ),
        )
    return _check(
        JOBS,
        plan.READY,
        _('No Job names the built-in feature, and all {count} captured schedule(s) have a live counterpart.').format(
            count=len(captured)
        ),
    )
