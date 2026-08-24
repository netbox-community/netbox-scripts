"""The irreversible step: capture what the later steps replay, then close what a plugin can."""

import contextlib
import datetime

from django.db import models
from django.utils.translation import gettext_lazy as _

from .. import activation
from ..choices import MigrationStateChoices, RevisionStatusChoices
from ..constants import PENDING_VERDICT_REVISION_STATUSES
from ..models import CustomScriptProject, CustomScriptProjectRevision
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from . import mapping
from . import source as legacy_source

__all__ = (
    'ACTIVATE_STEP',
    'STEP',
    'CutoverRefused',
    'activate_staged',
    'enter_cutover',
    'require_staged',
    'unservable_projects',
)

STEP = 'cutover'
ACTIVATE_STEP = 'activate'

# Everything activation can refuse with. One project failing must leave the rest to activate, so
# these are recorded as an outcome rather than allowed to end the pass.
_ACTIVATION_FAILURES = (ActivationError, RevisionCorruptError, StorageError, OSError)

# Marks an input value that cannot be written to the journal, so it is reported rather than
# silently replayed as absent.
_UNRENDERABLE = object()


class CutoverRefused(Exception):
    """The cutover cannot begin in the state the installation is in."""


def enter_cutover(run):
    """
    Record the cutover state, capture every reference the later steps replay, and close what closes.

    Captures once. Closing is idempotent. Returns the counts recorded on the run, and returns them
    unchanged without touching anything when the step has already completed. Raises CutoverRefused
    when the run is not in a state that may cross, while a built-in Script job is still running, or
    when a Project this migration mapped could serve nothing on the far side.
    """
    if run is None:
        raise CutoverRefused(_('Nothing has been staged yet, so there is nothing to cut over to.'))
    if run.state not in (MigrationStateChoices.STAGING, MigrationStateChoices.CUTOVER):
        raise CutoverRefused(_('A migration in the {state} state cannot enter the cutover.').format(state=run.state))
    if run.step_done(STEP):
        return run.recorded_counts(STEP)
    if running := list(legacy_source.running_script_jobs().values_list('pk', flat=True)):
        raise CutoverRefused(
            _('{count} built-in Script job(s) are still running: {keys}. Wait for them to finish.').format(
                count=len(running), keys=', '.join(str(key) for key in running)
            )
        )
    if blocked := unservable_projects(run):
        raise CutoverRefused(
            _(
                '{count} Custom Script Project(s) could serve nothing after the cutover: {detail}. '
                'Wait for a verdict or fix the source and stage again, because nothing comes back '
                'across this fence.'
            ).format(
                count=len(blocked),
                detail=', '.join(f'{entry["project_key"]} {entry["reason"]}' for entry in blocked),
            )
        )

    warnings = _capture(run)
    counts = _close(run.journal)
    if run.state == MigrationStateChoices.STAGING:
        run.advance(MigrationStateChoices.CUTOVER)
    run.complete_step(STEP, counts, warnings)
    return counts


def unservable_projects(run):
    """
    Return one entry per mapped Project that could not be put into service, empty when every one can.

    Each entry is {'project_key', 'reason'}. Reads only, and covers the modules the map names rather
    than the ones it could not map. Checks revision state rather than stored content, and returns
    empty once the fence has captured.
    """
    # The capture commits the map before anything closes, so a run holding one is already past
    # the point where refusing could do anything but strand it.
    if mapping.recorded(run) is not None:
        return []
    keys = mapping.project_keys(mapping.build_map())
    projects = {project.key: project for project in CustomScriptProject.objects.filter(key__in=keys)}
    by_project = {}
    columns = CustomScriptProjectRevision.objects.only('pk', 'project_id', 'status', 'created')
    for revision in columns.filter(project__key__in=keys).order_by('-created', '-pk'):
        by_project.setdefault(revision.project_id, []).append(revision)
    blocked = []
    for key in keys:
        project = projects.get(key)
        if project is None:
            blocked.append({'project_key': key, 'reason': _('has not been staged')})
            continue
        revisions = by_project.get(project.pk, [])
        if not revisions:
            blocked.append({'project_key': key, 'reason': _('holds no revision')})
            continue
        # Serving anything is enough: an older revision keeps serving across the fence, whatever
        # activation would later do with the pointer.
        if project.active_revision_id is not None:
            continue
        if not any(revision.status == RevisionStatusChoices.VALID for revision in revisions):
            blocked.append({'project_key': key, 'reason': _reason_for(revisions[0])})
    return blocked


def _reason_for(newest):
    """Say why a project cannot serve, separating a verdict still coming from one already reached."""
    status = dict(RevisionStatusChoices)[newest.status]
    if newest.status in PENDING_VERDICT_REVISION_STATUSES:
        return _('is still awaiting a verdict on its newest revision, which is {status}').format(status=status)
    return _('has no valid revision to activate and its newest is {status}').format(status=status)


def require_staged(run):
    """Return the run, raising CutoverRefused unless the fence and the map it froze are recorded."""
    if run is None or not run.step_done(STEP):
        raise CutoverRefused(_('The cutover has not been entered yet, so this step cannot run.'))
    if mapping.recorded(run) is None:
        # Every later pass replays this rather than deriving one, so a fence without it is unusable.
        raise CutoverRefused(_('The cutover recorded no plugin map, so no later step can replay it.'))
    return run


def activate_staged(run):
    """
    Put every Project this migration staged into service, and return one outcome each.

    Safe to run again: a project already serving its newest revision has its rows repaired, and
    nothing is written where nothing is wrong. Raises CutoverRefused before the fence.
    """
    require_staged(run)
    keys = mapping.project_keys(mapping.recorded(run))
    results = [
        _activate_project(project) for project in CustomScriptProject.objects.filter(key__in=keys).order_by('key')
    ]
    run.record_step(ACTIVATE_STEP, projects=_merged_outcomes(run, results))
    return results


def _merged_outcomes(run, results):
    """Return the recorded activation outcomes with this run's results merged in, by project key."""
    # Verification scopes itself by this record, and a re-run can cover fewer Projects.
    recorded = run.journal.get('steps', {}).get(ACTIVATE_STEP, {}).get('projects') or []
    merged = {entry['project_key']: entry for entry in recorded if entry.get('project_key')}
    merged.update({result['project_key']: result for result in results})
    return [merged[key] for key in sorted(merged)]


def _activate_project(project):
    """Activate the revision one project should serve, reporting rather than raising on refusal."""
    newest = project.revisions.order_by('-created').first()
    if newest is None:
        return _outcome(project, None, 'holds no revision')
    # The pointer field, not the current_revision property, which falls back to the newest attempt
    # and would report an unactivated project as though it were serving.
    if project.active_revision_id == newest.pk:
        return _activated(project, newest, 'was already serving this revision')
    candidate = project.revisions.filter(status=RevisionStatusChoices.VALID).order_by('-created').first()
    if candidate is None:
        # Retired is deliberately not accepted here: preferring it over an older valid revision
        # would serve a revision the project had already stood down from.
        return _outcome(project, newest.pk, f'has no valid revision to activate, its newest is {newest.status}')
    return _activated(project, candidate, 'activated')


def _activated(project, revision, outcome):
    """Activate one revision and return its outcome, or the refusal in its place."""
    try:
        activation.activate_revision(revision)
    except _ACTIVATION_FAILURES as error:
        return _outcome(project, revision.pk, f'could not be activated: {error}')
    return _outcome(project, revision.pk, outcome)


def _outcome(project, revision_pk, outcome):
    """Return one project's activation result in the shape the Job records."""
    return {'project_key': project.key, 'revision_pk': revision_pk, 'outcome': outcome}


def _capture(run):
    """Journal every reference the later steps replay, skipping whatever is already recorded."""
    # Skipped rather than refreshed: a second capture would read the closed state back as the original.
    warnings = []
    journal = run.journal
    if 'permissions' not in journal:
        journal['permissions'] = _capture_permissions()
    if 'event_rules' not in journal:
        journal['event_rules'] = _capture_event_rules()
    if 'schedules' not in journal:
        journal['schedules'], warnings = _capture_schedules()
    if 'mapping' not in journal:
        journal['mapping'] = mapping.build_map()
    run.save(update_fields=('journal', 'last_updated'))
    return warnings


def _capture_permissions():
    """Record every permission granting an action on the built-in feature, with who holds it."""
    legacy = {object_type.pk for object_type in legacy_source.legacy_object_types()}
    captured = []
    for permission in legacy_source.legacy_permissions().prefetch_related('object_types', 'users', 'groups'):
        types = list(permission.object_types.all())
        captured.append(
            {
                'pk': permission.pk,
                'name': permission.name,
                'description': permission.description,
                'enabled': permission.enabled,
                'actions': list(permission.actions),
                'constraints': permission.constraints,
                'users': sorted(user.pk for user in permission.users.all()),
                'groups': sorted(group.pk for group in permission.groups.all()),
                'legacy_object_types': sorted(_label(item) for item in types if item.pk in legacy),
                'other_object_types': sorted(_label(item) for item in types if item.pk not in legacy),
            }
        )
    return captured


def _capture_event_rules():
    """Record every Event Rule naming the built-in feature, as an action or as a source."""
    legacy = {object_type.pk for object_type in legacy_source.legacy_object_types()}
    captured = []
    for rule in legacy_source.legacy_event_rules().prefetch_related('object_types'):
        sources = [_label(item) for item in rule.object_types.all() if item.pk in legacy]
        captured.append(
            {
                'pk': rule.pk,
                'name': str(rule),
                'enabled': rule.enabled,
                'action_type': rule.action_type,
                # Set only where the action names the built-in feature, which is independent of
                # whether the rule also uses it as an event source.
                'action_object_id': rule.action_object_id if rule.action_object_type_id in legacy else None,
                'legacy_source_types': sorted(sources),
            }
        )
    return captured


def _capture_schedules():
    """Record every waiting built-in Script job with what a replay needs, and what could not be read."""
    captured, warnings = [], []
    for job in legacy_source.enqueued_script_jobs().select_related('object_type'):
        entry, dropped = _capture_schedule(job)
        if entry is None:
            warnings.append(
                f'Job {job.pk} ("{job.name}") is queued but its task is no longer in the queue, so its '
                f'input could not be read. Recreate it by hand after the cutover.'
            )
            continue
        if dropped:
            warnings.append(
                f'Job {job.pk} ("{job.name}") has input that cannot be recorded ({", ".join(dropped)}), '
                f'so it will not be recreated with those values.'
            )
        captured.append(entry)
    return captured, warnings


def _capture_schedule(job):
    """Return one waiting job's replay parameters and the input names dropped, or None if RQ lost it."""
    import django_rq
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    queue = django_rq.get_queue(job.queue_name)
    try:
        task = RQJob.fetch(str(job.job_id), connection=queue.connection)
    except NoSuchJobError:
        return None, []
    # Input lives only here. Job.enqueue() hands data and commit to the RQ task and keeps them off
    # the row, so the row alone cannot say what a scheduled run was going to do.
    kwargs = task.kwargs or {}
    data, dropped = _json_safe(kwargs.get('data') or {})
    entry = {
        'job_pk': job.pk,
        'legacy_script_pk': job.object_id,
        'legacy_object_type': _label(job.object_type),
        'name': job.name,
        'data': data,
        'commit': bool(kwargs.get('commit', True)),
        'scheduled': job.scheduled.isoformat() if job.scheduled else None,
        'interval': job.interval,
        'queue_name': job.queue_name,
        'job_timeout': task.timeout,
        'notifications': job.notifications,
        'user_pk': job.user_id,
    }
    return entry, dropped


def _json_safe(data):
    """Return script input as JSON, replacing a model instance with its key, and what was dropped."""
    safe, dropped = {}, []
    for name, value in data.items():
        rendered = _render(value)
        if rendered is _UNRENDERABLE:
            dropped.append(name)
        else:
            safe[name] = rendered
    return safe, dropped


def _render(value):
    """Return one input value in a form the journal can hold, or _UNRENDERABLE."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime.datetime | datetime.date):
        return value.isoformat()
    if isinstance(value, models.Model):
        # A form rebuilt from the key resolves it back to this instance, which is what ObjectVar does.
        return value.pk
    if isinstance(value, models.QuerySet | list | tuple):
        rendered = [_render(item) for item in value]
        return _UNRENDERABLE if any(item is _UNRENDERABLE for item in rendered) else rendered
    # An uploaded file is the case this exists for: it is gone once the request ended.
    return _UNRENDERABLE


def _close(journal):
    """Close every door available to a plugin, and report how many of each it closed."""
    return {
        'permissions': _disable_permissions(journal['permissions']),
        'event_rules': _disable_event_rules(journal['event_rules']),
        'schedules': _cancel_schedules(journal['schedules']),
        'auto_sync': _drop_auto_sync(journal),
    }


def _disable_permissions(captured):
    """Withdraw every captured grant on the built-in feature, and report how many are withdrawn."""
    from users.models import ObjectPermission

    keys = [entry['pk'] for entry in captured]
    # This is the closest a plugin has to the write and execution fence. It withdraws every grant
    # NetBox's own permissions UI can make and nothing more: a superuser still passes, and so does
    # anything DEFAULT_PERMISSIONS or a plain Django permission grants.
    ObjectPermission.objects.filter(pk__in=keys, enabled=True).update(enabled=False)
    # The state rather than the rows this attempt changed, so a resumed pass reports the same total.
    return ObjectPermission.objects.filter(pk__in=keys, enabled=False).count()


def _disable_event_rules(captured):
    """Take every captured Event Rule out of service, and report how many are out of service."""
    from extras.models import EventRule

    keys = [entry['pk'] for entry in captured]
    EventRule.objects.filter(pk__in=keys, enabled=True).update(enabled=False)
    return EventRule.objects.filter(pk__in=keys, enabled=False).count()


def _cancel_schedules(captured):
    """Fail every captured job closed and drop its task, so nothing queued can still execute."""
    import django_rq
    from rq.exceptions import NoSuchJobError
    from rq.job import Job as RQJob

    from core.choices import JobStatusChoices
    from core.models import Job

    cancelled = 0
    for entry in captured:
        job = Job.objects.filter(pk=entry['job_pk'], status__in=legacy_source.cancellable_statuses()).first()
        if job is None:
            continue
        queue = django_rq.get_queue(job.queue_name)
        with contextlib.suppress(NoSuchJobError):
            RQJob.fetch(str(job.job_id), connection=queue.connection).delete()
        # terminate() rather than an update, so the owner of a scheduled run is notified that it
        # was cancelled. There is no cancelled status, so this fails closed.
        job.terminate(
            JobStatusChoices.STATUS_FAILED,
            error='Cancelled by the Custom Scripts migration cutover. The plugin recreates this run.',
        )
        cancelled += 1
    return cancelled


def _drop_auto_sync(journal):
    """Deregister built-in script source from synchronization, recording what it deregistered."""
    # Scoped to script modules: a report is not this migration's, so its source keeps syncing.
    keys = legacy_source.legacy_script_module_keys()
    records = legacy_source.legacy_auto_sync_records(module_pks=keys)
    # Journalled like the other three closures, so an operator restoring by hand has it to read.
    journal['auto_sync'] = sorted(records.values_list('object_id', flat=True))
    deleted, _by_model = records.delete()
    return deleted


def _label(object_type):
    """Return one content type as the app-qualified label the journal records."""
    return f'{object_type.app_label}.{object_type.model}'
