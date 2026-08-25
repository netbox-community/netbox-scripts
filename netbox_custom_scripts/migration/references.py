"""Moving the references an installation holds off the built-in Custom Scripts onto plugin rows."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.translation import gettext_lazy as _

from ..execution import ScriptNotExecutableError, load_script_class
from ..runtime.exceptions import ScriptResolutionError
from ..storage.exceptions import StorageError
from . import cutover, mapping

__all__ = (
    'ACTION_SLUG',
    'EVENT_RULES_STEP',
    'HISTORY_STEP',
    'PERMISSIONS_STEP',
    'SCHEDULES_STEP',
    'recreate_schedules',
    'repoint_event_rules',
    'repoint_job_history',
    'repoint_permissions',
)

# Declared here rather than imported from event_rules.py, so this tier does not become a second
# loader of a module PluginConfig is meant to resolve on its own.
ACTION_SLUG = 'netbox_custom_scripts.run'

EVENT_RULES_STEP = 'repoint_event_rules'
PERMISSIONS_STEP = 'repoint_permissions'
HISTORY_STEP = 'repoint_job_history'
SCHEDULES_STEP = 'recreate_schedules'

# Which plugin model each built-in one's references move to.
_TYPE_MAP = {
    'extras.script': 'customscript',
    'extras.scriptmodule': 'customscriptproject',
}

# Which granted actions have a counterpart, per built-in type. The plugin's codenames were chosen to
# match, so there is nothing to rename. An action absent here is reported rather than approximated.
_ACTIONS = {
    'extras.script': frozenset({'view', 'change', 'run'}),
    'extras.scriptmodule': frozenset({'view', 'add', 'change', 'delete'}),
}

# Appended to the name of the permission created when a mixed one is split.
_SIBLING_SUFFIX = ' (Custom Scripts)'

# Marks a captured schedule whose time has passed and which must not simply be run instead.
_UNSCHEDULABLE = object()

# Everything a schedule can refuse to be replayed with: the script cannot run, or its revision
# cannot give up the class. The run view treats each of these as a reason rather than a crash.
_REPLAY_FAILURES = (ScriptNotExecutableError, ScriptResolutionError, StorageError, OSError)


def repoint_event_rules(run):
    """
    Point every captured Event Rule at the plugin, and re-enable the ones fully moved.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. A rule that cannot be moved is left disabled, named in a warning, and leaves
    the step incomplete so a later run covers it. Raises CutoverRefused until every migrated Project
    is serving a revision.
    """
    from extras.models import EventRule

    _require_serving(run)
    if run.step_done(EVENT_RULES_STEP):
        return run.recorded_counts(EVENT_RULES_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.recorded(run))
    plugin_types = _plugin_types()
    mapped = _mapped_script_keys(run)
    counts = {'actions': 0, 'sources': 0, 'restored': 0, 'unresolved': 0, 'withdrawn': 0}
    warnings = []
    for entry in run.journal.get('event_rules', []):
        rule = EventRule.objects.filter(pk=entry['pk']).first()
        if rule is None:
            continue
        with transaction.atomic():
            moved, counted, refusal = _repoint_action(rule, entry, resolved, plugin_types, mapped)
            if counted:
                counts[counted] += 1
            if refusal:
                warnings.append(refusal)
            _repoint_sources(rule, entry, plugin_types, counts)
            # Only a fully moved rule: re-enabling one pointing at nothing would fail at dispatch.
            if moved and entry['enabled'] and not rule.enabled:
                rule.enabled = True
                rule.save(update_fields=('enabled',))
                counts['restored'] += 1
    if counts['unresolved']:
        # Left open on purpose: the operator repairs what each warning names and runs this again.
        return counts, warnings
    run.complete_step(EVENT_RULES_STEP, counts, warnings)
    return counts, warnings


def repoint_permissions(run):
    """
    Move every captured grant on the built-in feature onto the plugin's own object types.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. A permission naming only the built-in feature is swapped in place, one that
    also names unrelated types keeps those and gains a sibling for the plugin, and one carrying
    constraints is reported and left alone. Raises CutoverRefused before activation.
    """
    from users.models import ObjectPermission

    _require_activated(run)
    if run.step_done(PERMISSIONS_STEP):
        return run.recorded_counts(PERMISSIONS_STEP), []

    plugin_types = _plugin_types()
    counts = {'swapped': 0, 'split': 0, 'constrained': 0, 'unmappable': 0}
    warnings = []
    for entry in run.journal.get('permissions', []):
        permission = ObjectPermission.objects.filter(pk=entry['pk']).first()
        if permission is None:
            continue
        if entry['constraints']:
            # Its filters name fields the plugin models do not have, so copying it would make
            # restrict() raise on every page that uses it and dropping them would widen access.
            counts['constrained'] += 1
            warnings.append(
                _(
                    'Permission "{name}" carries constraints on the built-in Custom Scripts, so it was '
                    'left withdrawn and untranslated. Recreate it by hand against the plugin models.'
                ).format(name=entry['name'])
            )
            continue
        actions, unmappable = _map_actions(entry)
        if not actions:
            # Moving it would leave an enabled permission granting nothing, so it is treated like a
            # constrained one: left withdrawn, and named.
            counts['unmappable'] += 1
            warnings.append(
                _(
                    'Permission "{name}" granted only {actions} on the built-in Custom Scripts, none of '
                    'which the plugin separates, so it was left withdrawn.'
                ).format(name=entry['name'], actions=', '.join(sorted(unmappable)))
            )
            continue
        if unmappable:
            warnings.append(
                _(
                    'Permission "{name}" granted {actions} on the built-in Custom Scripts, which the plugin '
                    'does not separate, so that grant was not preserved.'
                ).format(name=entry['name'], actions=', '.join(sorted(unmappable)))
            )
        targets = [plugin_types[label].pk for label in entry['legacy_object_types'] if label in plugin_types]
        with transaction.atomic():
            if entry['other_object_types']:
                warnings.extend(_split_permission(run, permission, entry, targets, actions))
                counts['split'] += 1
            else:
                _swap_permission(permission, entry, targets, actions)
                counts['swapped'] += 1
    # Recorded whatever it left behind, unlike the other three: a constrained or unmappable
    # permission is withdrawn for a person to recreate, and no re-run of this pass changes that.
    run.complete_step(PERMISSIONS_STEP, counts, warnings)
    return counts, warnings


def repoint_job_history(run):
    """
    Move the built-in Scripts' Job history onto the Custom Scripts that replaced them.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. A job whose script does not resolve is left where it is, reported, and leaves
    the step incomplete so a later run covers it. A job naming a module, or a class that left its
    file, has no counterpart at all and does not. Raises CutoverRefused until every migrated
    Project is serving a revision.
    """
    from core.models import Job

    _require_serving(run)
    if run.step_done(HISTORY_STEP):
        return run.recorded_counts(HISTORY_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.recorded(run))
    legacy = _legacy_type('extras.script')
    target = _plugin_types()['extras.script']
    counts = {'moved': 0, 'unresolved': 0, 'modules': 0, 'outstanding': 0}
    warnings = []
    for legacy_pk, script in resolved.items():
        # One statement per script rather than one per job. update() fires no signals and skips
        # clean(), which is what this needs: the row is being corrected, not authored.
        counts['moved'] += Job.objects.filter(object_type=legacy, object_id=legacy_pk).update(
            object_type_id=target.pk, object_id=script.pk
        )
    # Whatever still names the built-in Script type is what did not resolve, because the updates
    # above moved everything that did. Scoped by object type rather than by key, since a job on a
    # built-in module can hold a key equal to a Script's and would otherwise be repointed at it.
    stranded = Job.objects.filter(object_type=legacy).values_list('object_id', flat=True).distinct()
    mapped = _mapped_script_keys(run)
    for legacy_pk in stranded:
        counts['unresolved'] += 1
        if legacy_pk not in mapped:
            warnings.append(
                _(
                    'Job history for built-in Custom Script {key} was left where it is, because that class '
                    'left its file and no Custom Script replaces it.'
                ).format(key=legacy_pk)
            )
            continue
        counts['outstanding'] += 1
        warnings.append(
            _(
                'Job history for built-in Custom Script {key} was left where it is, because no Custom '
                'Script resolves to it. Deleting that Script would take its jobs with it.'
            ).format(key=legacy_pk)
        )
    counts['modules'] = Job.objects.filter(object_type=_legacy_type('extras.scriptmodule')).count()
    if counts['modules']:
        warnings.append(
            _(
                '{count} Job(s) name a built-in script module rather than a Script, and a Custom Script '
                'Project holds no jobs, so they were left where they are.'
            ).format(count=counts['modules'])
        )
    if counts['outstanding']:
        # A module Job and a departed class's history are excluded, because no plugin row will
        # ever hold either one and the step could then never complete.
        return counts, warnings
    run.complete_step(HISTORY_STEP, counts, warnings)
    return counts, warnings


def recreate_schedules(run):
    """
    Put every schedule the fence cancelled back into service against the Custom Script.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. Each recreated job is recorded against the captured one inside the
    transaction that creates it. Anything that cannot be replayed is reported and skipped, and the
    part of that a later run could still recover counts as outstanding, which leaves the step
    incomplete. Raises CutoverRefused until every migrated Project is serving a revision.
    """
    _require_serving(run)
    if run.step_done(SCHEDULES_STEP):
        return run.recorded_counts(SCHEDULES_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.recorded(run))
    mapped = _mapped_script_keys(run)
    recreated = run.journal.setdefault('recreated_schedules', {})
    counts = {'recreated': 0, 'skipped': 0, 'shifted': 0, 'outstanding': 0}
    warnings = []
    for entry in run.journal.get('schedules', []):
        if str(entry['job_pk']) in recreated:
            continue
        # By type too: a module key can equal a Script's, so a bare lookup can hit the wrong row.
        if entry.get('legacy_object_type') != 'extras.script':
            counts['skipped'] += 1
            warnings.append(
                _(
                    'Schedule "{name}" ran a built-in script module rather than a Script, which no Custom '
                    'Script corresponds to, so it was not recreated.'
                ).format(name=entry['name'])
            )
            continue
        script = resolved.get(entry['legacy_script_pk'])
        if script is None:
            counts['skipped'] += 1
            if entry['legacy_script_pk'] in mapped:
                counts['outstanding'] += 1
                warnings.append(
                    _(
                        'Schedule "{name}" ran built-in Custom Script {key}, which no Custom Script '
                        'resolves to, so it was not recreated.'
                    ).format(name=entry['name'], key=entry['legacy_script_pk'])
                )
                continue
            warnings.append(
                _(
                    'Schedule "{name}" ran built-in Custom Script {key}, whose class left its file, so no '
                    'Custom Script will ever replace it and it was not recreated. Schedule it again by '
                    'hand against whatever replaces it.'
                ).format(name=entry['name'], key=entry['legacy_script_pk'])
            )
            continue
        schedule_at, shifted = _replay_schedule(entry)
        if schedule_at is _UNSCHEDULABLE:
            counts['skipped'] += 1
            warnings.append(
                _(
                    'Schedule "{name}" was due at {due}, which has passed, so it was not recreated rather '
                    'than run at once. Schedule it again by hand.'
                ).format(name=entry['name'], due=entry['scheduled'])
            )
            continue
        user = _user(entry['user_pk'])
        if entry['user_pk'] and user is None:
            warnings.append(
                _(
                    'Schedule "{name}" belonged to a user who no longer exists, so it was recreated with '
                    'no owner and its completion notifies nobody.'
                ).format(name=entry['name'])
            )
        try:
            _recreate(run, entry, script, schedule_at, user, recreated)
        except _REPLAY_FAILURES as error:
            counts['skipped'] += 1
            counts['outstanding'] += 1
            warnings.append(
                _('Schedule "{name}" could not be recreated against {script}: {error}').format(
                    name=entry['name'], script=script, error=error
                )
            )
            continue
        except ValidationError as invalid:
            counts['skipped'] += 1
            warnings.append(
                _(
                    'Schedule "{name}" no longer supplies valid input for {script}, so it was not recreated: {errors}'
                ).format(name=entry['name'], script=script, errors=invalid.messages)
            )
            continue
        counts['recreated'] += 1
        if entry['scheduled'] is None:
            warnings.append(
                _(
                    'Schedule "{name}" was queued rather than scheduled, so it was recreated to run at once '
                    'against {script}, with the commit setting it was queued with.'
                ).format(name=entry['name'], script=script)
            )
        if shifted:
            counts['shifted'] += 1
            warnings.append(
                _(
                    'Schedule "{name}" was due at {due}, which has passed, so its recurrence starts now '
                    'and keeps its {interval} minute interval.'
                ).format(name=entry['name'], due=entry['scheduled'], interval=entry['interval'])
            )
    if counts['outstanding']:
        # A schedule the operator cannot get back, a past-due one-shot or input naming a deleted
        # object, is skipped rather than outstanding, or the step could never complete.
        return counts, warnings
    run.complete_step(SCHEDULES_STEP, counts, warnings)
    return counts, warnings


def _replay_schedule(entry):
    """Return the start time one captured schedule is replayed with, and whether its phase moved."""
    if not entry['scheduled']:
        # It was pending rather than scheduled, so it was already due.
        return None, False
    scheduled = parse_datetime(entry['scheduled'])
    if scheduled and scheduled > timezone.now():
        return scheduled, False
    # rq runs a past-due job the moment it is enqueued, and a migration must not run an operator's
    # script unasked. A recurrence keeps its interval and starts now, a one-shot is refused.
    return (None, True) if entry['interval'] else (_UNSCHEDULABLE, False)


def _recreate(run, entry, script, schedule_at, user, recreated):
    """Validate one captured schedule's input and enqueue it, recording the new job with it."""
    # Locally, because jobs.py imports this tier.
    from ..jobs import CustomScriptJob

    instance = load_script_class(script)()
    # Through the form, because that is what turns the journal's keys back into the model instances
    # an ObjectVar resolves to.
    form = instance.as_form({**entry['data'], '_commit': entry['commit']})
    if not form.is_valid():
        raise ValidationError(_form_errors(form))
    data = dict(form.cleaned_data)
    # Popped exactly as the run view pops them, so no execution parameter reaches the script as a
    # variable value. The schedule itself comes from the captured Job row, not from these.
    commit = data.pop('_commit', True)
    for name in ('_schedule_at', '_interval', '_notifications'):
        data.pop(name, None)
    with transaction.atomic():
        job = CustomScriptJob.enqueue_run(
            script,
            data=data,
            commit=commit,
            user=user,
            schedule_at=schedule_at,
            interval=entry['interval'],
            notifications=entry['notifications'],
            queue_name=entry['queue_name'],
            job_timeout=entry['job_timeout'],
        )
        # Recorded with the job rather than after it, so the queue can never hold a task the journal
        # has not claimed. Job.enqueue() hands the task over in a commit hook.
        recreated[str(entry['job_pk'])] = job.pk
        run.save(update_fields=('journal', 'last_updated'))


def _form_errors(form):
    """Return one run form's errors as the plain strings a warning can carry."""
    return [f'{field}: {", ".join(errors)}' for field, errors in form.errors.items()]


def _user(user_pk):
    """Return the user a captured schedule belonged to, or None once they are gone."""
    return get_user_model().objects.filter(pk=user_pk).first() if user_pk else None


def _mapped_script_keys(run):
    """Return the built-in Script keys the frozen map covers, which is all any pass can resolve."""
    # Everything else is a class that left its file. build_map excludes it and the map is frozen,
    # so no repair and no re-run ever brings it back, and holding a step open for one would leave
    # a migration that can never close.
    return {entry['legacy_pk'] for entry in mapping.recorded(run)['scripts']}


def _require_activated(run):
    """Return the run, raising CutoverRefused unless activation has recorded its step on it."""
    cutover.require_staged(run)
    if not run.step_done(cutover.ACTIVATE_STEP):
        raise cutover.CutoverRefused(
            _('The staged Projects have not been activated yet, so there are no Custom Scripts to point at.')
        )
    return run


def _require_serving(run):
    """Return the run, raising CutoverRefused unless every migrated Project is serving a revision."""
    _require_activated(run)
    # Not taken by the permission pass, which maps object types and names no Custom Script. The
    # fence withdrew every grant on the built-in feature, so blocking that pass over an unrelated
    # Project would keep every non-superuser locked out of both sides until it was repaired.
    if outstanding := cutover.projects_not_serving(run):
        raise cutover.CutoverRefused(
            _(
                '{count} migrated Custom Script Project(s) are serving nothing: {keys}. Put each one into '
                'service and run the activation again, because a reference can only name a Custom Script '
                'that exists.'
            ).format(count=len(outstanding), keys=', '.join(outstanding))
        )
    return run


def _plugin_types():
    """Return the plugin object type each built-in type's references move to, by its label."""
    from core.models import ObjectType

    from ..models import CustomScript, CustomScriptProject

    models = {'customscript': CustomScript, 'customscriptproject': CustomScriptProject}
    return {label: ObjectType.objects.get_for_model(models[name]) for label, name in _TYPE_MAP.items()}


def _repoint_action(rule, entry, resolved, plugin_types, mapped):
    """
    Point one rule's action at the plugin, writing nothing it cannot move.

    Returns whether the action needs nothing further, the count key to increment, and the refusal to
    report, so the caller owns its own counts and warnings as the other three passes do.
    """
    if entry['action_object_id'] is None or rule.action_type == ACTION_SLUG:
        return True, None, None
    script = resolved.get(entry['action_object_id'])
    if script is None:
        if entry['action_object_id'] not in mapped:
            refusal = _(
                'Event rule "{name}" runs built-in Custom Script {key}, whose class left its file, so no '
                'Custom Script will ever replace it and the rule was left withdrawn. Delete it, or point '
                'it at a script yourself.'
            ).format(name=entry['name'], key=entry['action_object_id'])
            return False, 'withdrawn', refusal
        refusal = _(
            'Event rule "{name}" runs built-in Custom Script {key}, which no Custom Script resolves to, '
            'so it was left withdrawn.'
        ).format(name=entry['name'], key=entry['action_object_id'])
        return False, 'unresolved', refusal
    if script.is_retired:
        # The action refuses a retired script at save time, so this would raise rather than write.
        refusal = _(
            'Event rule "{name}" resolves to {script}, which is retired and can never run again, so the '
            'rule was left withdrawn.'
        ).format(name=entry['name'], script=script)
        return False, 'unresolved', refusal
    rule.action_type = ACTION_SLUG
    rule.action_object_type = plugin_types['extras.script']
    rule.action_object_id = script.pk
    try:
        rule.full_clean()
    except ValidationError as invalid:
        # A rule can be invalid for reasons that predate this migration, an event type its own
        # plugin stopped registering being the likely one. One rule must not end the pass.
        refusal = _(
            'Event rule "{name}" cannot be saved, so it was left withdrawn: {errors}. Correct or delete '
            'the rule, then run this pass again.'
        ).format(name=entry['name'], errors=invalid.messages)
        return False, 'unresolved', refusal
    rule.save(update_fields=('action_type', 'action_object_type', 'action_object_id'))
    return True, 'actions', None


def _repoint_sources(rule, entry, plugin_types, counts):
    """Replace the built-in types one rule watches with the plugin's."""
    labels = [label for label in entry['legacy_source_types'] if label in plugin_types]
    if not labels:
        return
    current = set(rule.object_types.values_list('pk', flat=True))
    legacy = {_legacy_type(label).pk for label in labels}
    if not legacy & current:
        # Already moved by an earlier attempt at this step.
        return
    wanted = {plugin_types[label].pk for label in labels}
    rule.object_types.remove(*sorted(legacy & current))
    rule.object_types.add(*sorted(wanted - current))
    counts['sources'] += 1


def _map_actions(entry):
    """Return the actions one captured permission keeps, and the ones with no counterpart."""
    permitted = set()
    for label in entry['legacy_object_types']:
        permitted |= _ACTIONS.get(label, frozenset())
    actions = [action for action in entry['actions'] if action in permitted]
    # A row's actions apply to every type it names, so the built-in row already granted each of
    # these on each of its types. Keeping the union mirrors it rather than inventing a distinction.
    return actions, set(entry['actions']) - permitted


def _swap_permission(permission, entry, targets, actions):
    """Move one permission that names nothing but the built-in feature onto the plugin types."""
    permission.object_types.set(targets)
    permission.actions = actions
    # The fence withdrew it. Nothing it grants names the built-in feature any more, so it goes back
    # into service in the state it was captured in.
    permission.enabled = entry['enabled']
    permission.save(update_fields=('actions', 'enabled'))


def _split_permission(run, permission, entry, targets, actions):
    """Strip the built-in types from one permission, give the plugin types a sibling, and warn."""
    from users.models import Group, ObjectPermission

    # Keyed on the captured permission rather than on the sibling's name, because ObjectPermission
    # names are not unique: two rows sharing one would otherwise adopt each other's sibling and
    # silently drop a set of users and groups.
    split = run.journal.setdefault('split_permissions', {})
    if str(entry['pk']) in split:
        return []
    legacy = [_legacy_type(label).pk for label in entry['legacy_object_types']]
    permission.object_types.remove(*legacy)
    # Its remaining types were never part of this migration, so the fence's withdrawal is undone.
    permission.enabled = entry['enabled']
    permission.save(update_fields=('enabled',))
    name = f'{permission.name}{_SIBLING_SUFFIX}'
    sibling = ObjectPermission.objects.create(
        # Truncated because the field is bounded and a long original would otherwise raise.
        name=name[: ObjectPermission._meta.get_field('name').max_length],
        description=entry['description'],
        enabled=entry['enabled'],
        actions=actions,
    )
    sibling.object_types.set(targets)
    # Resolved first: set() inserts a raw key unchecked, and a deferred FK fails the pass at commit.
    users, lost_users = _live_keys(get_user_model(), entry['users'])
    groups, lost_groups = _live_keys(Group, entry['groups'])
    sibling.users.set(users)
    sibling.groups.set(groups)
    split[str(entry['pk'])] = sibling.pk
    run.save(update_fields=('journal', 'last_updated'))
    if not (lost_users or lost_groups):
        return []
    return [
        _(
            'Permission "{name}" was split, and {count} of the users and groups it was granted to no '
            'longer exist, so the new "{sibling}" does not carry them.'
        ).format(name=entry['name'], count=len(lost_users) + len(lost_groups), sibling=sibling.name)
    ]


def _live_keys(model, keys):
    """Return the keys of the given model that still exist, and the ones that do not."""
    live = set(model.objects.filter(pk__in=keys).values_list('pk', flat=True))
    return sorted(live), sorted(set(keys) - live)


def _legacy_type(label):
    """Return the content type one recorded label names."""
    from django.contrib.contenttypes.models import ContentType

    # get_by_natural_key: the manager caches per database alias, where our own cache would go stale.
    return ContentType.objects.get_by_natural_key(*label.split('.'))
