"""Reporting whether a migration landed, by reading only."""

from django.utils.translation import gettext_lazy as _

from core.choices import JobStatusChoices
from core.models import Job

from ..models import MigrationRun, NetBoxScript, ScriptProject
from . import cleanup, cutover, mapping, plan
from . import references as legacy_references
from . import source as legacy_source

__all__ = ('verify',)

# The name each check reports under, which is what reaches the Job log and the report data.
MODULES = 'modules'
SCRIPTS = 'scripts'
EVENT_RULES = 'event_rules'
PERMISSIONS = 'permissions'
JOBS = 'jobs'

# A recreation that ended either way did not take.
_FAILED_JOB_STATUSES = (JobStatusChoices.STATUS_ERRORED, JobStatusChoices.STATUS_FAILED)

# A row in one of these is still owed a run, so it is still owed a queue task.
_WAITING_JOB_STATUSES = (JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED)


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
        ScriptProject.objects.filter(key__in=keys, active_revision__isnull=False).values_list('key', flat=True)
    )
    if missing := [key for key in keys if key not in serving]:
        return _check(
            MODULES,
            plan.BLOCKING,
            _('{count} of {total} migrated Project(s) serve no revision: {keys}.').format(
                count=len(missing), total=len(keys), keys=', '.join(missing)
            ),
        )
    if unmapped := (mapping.recorded(run) or {}).get('unmapped') or []:
        return _check(
            MODULES,
            plan.BLOCKING,
            _('{count} built-in module(s) have no plugin identity and were never staged: {paths}.').format(
                count=len(unmapped), paths=', '.join(sorted(entry['path'] for entry in unmapped))
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
    """Every built-in Custom Script has a live Script, read from whichever side still holds them."""
    if not run.step_done(cutover.ACTIVATE_STEP):
        return _check(SCRIPTS, plan.WARNING, _('No Script exists yet, because nothing has been activated.'))
    if live_modules:
        # The frozen map where there is one, so a partial cleanup cannot move the comparison.
        plugin_map = mapping.recorded(run) or mapping.build_map(modules=live_modules, resolve_existing=True)
        resolved, unresolved = mapping.resolve_scripts(plugin_map)
        if unresolved:
            return _check(
                SCRIPTS,
                plan.BLOCKING,
                _('{count} built-in Custom Script(s) resolve to no Script: {names}.').format(
                    count=len(unresolved), names=', '.join(sorted(entry['legacy_name'] for entry in unresolved))
                ),
                source=_('the built-in rows'),
            )
        if retired := sorted(row.class_name for row in resolved.values() if row.is_retired):
            return _check(
                SCRIPTS,
                plan.BLOCKING,
                _('{count} Script(s) that replace a built-in one are retired: {names}.').format(
                    count=len(retired), names=', '.join(retired)
                ),
                source=_('the built-in rows'),
            )
        return _check(
            SCRIPTS,
            plan.READY,
            _('All {count} built-in Custom Script(s) resolve to a Script that can run.').format(count=len(resolved)),
            source=_('the built-in rows'),
        )
    # The built-in rows are gone, so what a Project publishes is the only remaining evidence.
    keys = _activated_keys(run)
    published = NetBoxScript.objects.filter(project__key__in=keys)
    # Only the Projects the map recorded a script for. One holding nothing but helper files was
    # never expected to publish, and staging declared no script file on it.
    expected = {entry['project_key'] for entry in (mapping.recorded(run) or {}).get('scripts') or []}
    if barren := sorted((set(keys) & expected) - set(published.values_list('project__key', flat=True))):
        return _check(
            SCRIPTS,
            plan.BLOCKING,
            _('The built-in rows are gone and {count} migrated Project(s) publish no Script: {keys}.').format(
                count=len(barren), keys=', '.join(barren)
            ),
        )
    if retired := published.filter(is_retired=True).count():
        return _check(
            SCRIPTS,
            plan.WARNING,
            _(
                'The built-in rows are gone. {count} of the {total} Script(s) the migrated Projects '
                'publish are retired.'
            ).format(count=retired, total=published.count()),
        )
    return _check(
        SCRIPTS,
        plan.READY,
        _('The built-in rows are gone and the migrated Projects publish {count} Script(s), none retired.').format(
            count=published.count()
        ),
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
    # Only a rule absent from the journal is a fault: one it captured was left on purpose.
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
    # Same split as the Event Rules check: a constrained one is left withdrawn on purpose.
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
    # Restated every run: recreating one is operator work that stays outstanding.
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


def _references_running():
    """Whether a reference pass is queued or running, so the queue is still being written."""
    from ..jobs import MigrationReferencesJob

    return MigrationReferencesJob.get_jobs().filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()


def _queueless_schedules(recreated, names):
    """
    Return which recreated schedules are waiting with no queue task behind them.

    Probes only the Job rows in a waiting status. Returns ([], False) for a queue it could not
    read.
    """
    import django_rq
    from redis.exceptions import RedisError
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    from utilities.rqworker import get_queue_for_model

    lost = []
    # Waiting rows only: a recurrence is re-enqueued as a new row each occurrence, so a terminal
    # one holding no task is the ordinary end state rather than a loss.
    waiting = (
        Job.objects.filter(pk__in=list(recreated.values()), status__in=_WAITING_JOB_STATUSES)
        .select_related('object_type')
        .only('pk', 'job_id', 'queue_name', 'object_type')
    )
    for job in waiting:
        # Queue.fetch_job() would be shorter and removes the id from the queue on a miss, which a
        # pass that promises to change nothing cannot do.
        name = job.queue_name or get_queue_for_model(job.object_type.model if job.object_type else None)
        try:
            RQJob.fetch(str(job.job_id), connection=django_rq.get_queue(name).connection)
        except NoSuchJobError:
            lost.append(names.get(job.pk) or str(job.pk))
        except (RedisError, KeyError):
            # Distinct from an absent task on purpose. Treating an outage as a loss would name
            # every migrated schedule as gone and send the operator to recreate all of them.
            return [], False
    return sorted(lost), True


def _verify_jobs(run):
    """No Job names the built-in feature, and every captured schedule has a live counterpart."""
    captured = run.journal.get('schedules') or []
    if not run.step_done(legacy_references.HISTORY_STEP):
        return _check(JOBS, plan.WARNING, _('The Job history has not been repointed yet.'))
    recreated = run.journal.get('recreated_schedules') or {}
    # A recorded number that has simply gone says nothing: a recurring job is re-enqueued as a new
    # row every occurrence and housekeeping deletes by age, so the first one is meant to disappear.
    # What is readable is a recreation that never happened, and one whose Job ended in failure.
    failed = set(
        Job.objects.filter(pk__in=list(recreated.values()), status__in=_FAILED_JOB_STATUSES).values_list(
            'pk', flat=True
        )
    )
    missing = [
        entry
        for entry in captured
        if str(entry['job_pk']) not in recreated or recreated[str(entry['job_pk'])] in failed
    ]
    # Read from the queue rather than the row, because the row is exactly what survives. The marker
    # is written inside the enqueue transaction and the task is handed over in a commit hook.
    by_pk = {recreated[str(entry['job_pk'])]: entry['name'] for entry in captured if str(entry['job_pk']) in recreated}
    lost, queue_read = ([], False) if _references_running() else _queueless_schedules(recreated, by_pk)
    if lost:
        return _check(
            JOBS,
            plan.BLOCKING,
            _(
                '{count} recreated schedule(s) are recorded as migrated but hold no task in the queue, so '
                'they will never run: {names}. Recreate each one by hand.'
            ).format(count=len(lost), names=', '.join(lost)),
            source=_('the journal and the queue'),
        )
    held = legacy_source.script_jobs()
    if held.exists():
        return _check(
            JOBS,
            plan.WARNING,
            _(
                '{count} Job(s) still name the built-in feature. Each one is history no Script Project '
                'can hold, so it stays where it is.'
            ).format(count=held.count()),
            source=_('the built-in rows'),
        )
    if not queue_read:
        return _check(
            JOBS,
            plan.WARNING,
            _('The queue was not read, so whether each recreated schedule holds a task is unknown.'),
            source=_('the journal alone'),
        )
    if missing:
        return _check(
            JOBS,
            plan.WARNING,
            _(
                'No Job names the built-in feature. {count} of the {total} captured schedule(s) were not '
                'recreated, or were recreated into a job that failed: {names}.'
            ).format(
                count=len(missing), total=len(captured), names=', '.join(sorted(entry['name'] for entry in missing))
            ),
            source=_('the journal and the live jobs'),
        )
    return _check(
        JOBS,
        plan.READY,
        _('No Job names the built-in feature, and all {count} captured schedule(s) have a live counterpart.').format(
            count=len(captured)
        ),
        source=_('the journal and the live jobs'),
    )
