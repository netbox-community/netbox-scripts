"""Moving the references an installation holds off the built-in Custom Scripts onto plugin rows."""

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

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

# Declared here rather than imported from event_rules.py, which imports netbox.event_rules at module
# level. That module is 4.7 only, so importing it would take this whole tier down on a 4.6 host.
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
    step has completed, so a resumed migration continues rather than repeats. A rule that cannot be
    moved is left disabled and named in a warning, because re-enabling one that points at nothing
    would fail at dispatch. Raises CutoverRefused before the staged Projects are activated.
    """
    from extras.models import EventRule

    _require_activated(run)
    if run.step_done(EVENT_RULES_STEP):
        return run.recorded_counts(EVENT_RULES_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.build_map())
    plugin_types = _plugin_types()
    serves_action = _host_serves_action()
    counts = {'actions': 0, 'sources': 0, 'restored': 0, 'unserved': 0}
    warnings = []
    for entry in run.journal.get('event_rules', []):
        rule = EventRule.objects.filter(pk=entry['pk']).first()
        if rule is None:
            continue
        with transaction.atomic():
            moved, counted, refusal = _repoint_action(rule, entry, resolved, plugin_types, serves_action)
            if counted:
                counts[counted] += 1
            if refusal:
                warnings.append(refusal)
            _repoint_sources(rule, entry, plugin_types, counts)
            # Only a rule with nothing left pointing at the built-in feature goes back into service.
            if moved and entry['enabled'] and not rule.enabled:
                rule.enabled = True
                rule.save(update_fields=('enabled',))
                counts['restored'] += 1
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
                f'Permission "{entry["name"]}" carries constraints on the built-in Custom Scripts, so it was '
                f'left withdrawn and untranslated. Recreate it by hand against the plugin models.'
            )
            continue
        actions, unmappable = _map_actions(entry)
        if not actions:
            # Moving it would leave an enabled permission granting nothing, so it is treated like a
            # constrained one: left withdrawn, and named.
            counts['unmappable'] += 1
            warnings.append(
                f'Permission "{entry["name"]}" granted only {", ".join(sorted(unmappable))} on the built-in '
                f'Custom Scripts, none of which the plugin separates, so it was left withdrawn.'
            )
            continue
        if unmappable:
            warnings.append(
                f'Permission "{entry["name"]}" granted {", ".join(sorted(unmappable))} on the built-in Custom '
                f'Scripts, which the plugin does not separate, so that grant was not preserved.'
            )
        targets = [plugin_types[label].pk for label in entry['legacy_object_types'] if label in plugin_types]
        with transaction.atomic():
            if entry['other_object_types']:
                _split_permission(run, permission, entry, targets, actions)
                counts['split'] += 1
            else:
                _swap_permission(permission, entry, targets, actions)
                counts['swapped'] += 1
    run.complete_step(PERMISSIONS_STEP, counts, warnings)
    return counts, warnings


def repoint_job_history(run):
    """
    Move the built-in Scripts' Job history onto the Custom Scripts that replaced them.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. Runs before anything is deleted, because a Script's jobs are a
    GenericRelation and go with it. A job whose script does not resolve is left where it is and
    reported, since an orphaned history row beats a wrong one. Raises CutoverRefused before
    activation.
    """
    from core.models import Job

    _require_activated(run)
    if run.step_done(HISTORY_STEP):
        return run.recorded_counts(HISTORY_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.build_map())
    legacy = _legacy_type('extras.script')
    target = _plugin_types()['extras.script']
    counts = {'moved': 0, 'unresolved': 0, 'modules': 0}
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
    for legacy_pk in stranded:
        counts['unresolved'] += 1
        warnings.append(
            f'Job history for built-in Custom Script {legacy_pk} was left where it is, because no Custom '
            f'Script resolves to it. Deleting that Script would take its jobs with it.'
        )
    counts['modules'] = Job.objects.filter(object_type=_legacy_type('extras.scriptmodule')).count()
    if counts['modules']:
        warnings.append(
            f'{counts["modules"]} Job(s) name a built-in script module rather than a Script, and a Custom '
            f'Script Project holds no jobs, so they were left where they are.'
        )
    run.complete_step(HISTORY_STEP, counts, warnings)
    return counts, warnings


def recreate_schedules(run):
    """
    Put every schedule the fence cancelled back into service against the Custom Script.

    Returns the counts and the warnings raised, and returns the recorded counts unchanged once the
    step has completed. Each recreated job is recorded against the captured one inside the
    transaction that creates it, because this is the one step that is not naturally idempotent.
    Anything that cannot be replayed is reported and skipped. Raises CutoverRefused before
    activation.
    """
    _require_activated(run)
    if run.step_done(SCHEDULES_STEP):
        return run.recorded_counts(SCHEDULES_STEP), []

    resolved, _unresolved = mapping.resolve_scripts(mapping.build_map())
    recreated = run.journal.setdefault('recreated_schedules', {})
    counts = {'recreated': 0, 'skipped': 0, 'shifted': 0}
    warnings = []
    for entry in run.journal.get('schedules', []):
        if str(entry['job_pk']) in recreated:
            continue
        script = resolved.get(entry['legacy_script_pk'])
        if script is None:
            counts['skipped'] += 1
            warnings.append(
                f'Schedule "{entry["name"]}" ran built-in Custom Script {entry["legacy_script_pk"]}, which no '
                f'Custom Script resolves to, so it was not recreated.'
            )
            continue
        schedule_at, shifted = _replay_schedule(entry)
        if schedule_at is _UNSCHEDULABLE:
            counts['skipped'] += 1
            warnings.append(
                f'Schedule "{entry["name"]}" was due at {entry["scheduled"]}, which has passed, so it was not '
                f'recreated rather than run at once. Schedule it again by hand.'
            )
            continue
        user = _user(entry['user_pk'])
        if entry['user_pk'] and user is None:
            warnings.append(
                f'Schedule "{entry["name"]}" belonged to a user who no longer exists, so it was recreated '
                f'with no owner and its completion notifies nobody.'
            )
        try:
            _recreate(run, entry, script, schedule_at, user, recreated)
        except _REPLAY_FAILURES as error:
            counts['skipped'] += 1
            warnings.append(f'Schedule "{entry["name"]}" could not be recreated against {script}: {error}')
            continue
        except ValidationError as invalid:
            counts['skipped'] += 1
            warnings.append(
                f'Schedule "{entry["name"]}" no longer supplies valid input for {script}, so it was not '
                f'recreated: {invalid.messages}'
            )
            continue
        counts['recreated'] += 1
        if shifted:
            counts['shifted'] += 1
            warnings.append(
                f'Schedule "{entry["name"]}" was due at {entry["scheduled"]}, which has passed, so its '
                f'recurrence starts now and keeps its {entry["interval"]} minute interval.'
            )
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


def _require_activated(run):
    """Return the run, raising CutoverRefused unless activation has recorded its step on it."""
    cutover.require_staged(run)
    if not run.step_done(cutover.ACTIVATE_STEP):
        raise cutover.CutoverRefused(
            'The staged Projects have not been activated yet, so there are no Custom Scripts to point at.'
        )
    return run


def _host_serves_action():
    """Return whether the host's Event Rule registry carries this plugin's action."""
    # A registry lookup rather than probing for the module, because what matters is whether OUR
    # action registered. Delete this and its callers when the floor reaches 4.7.
    try:
        from netbox.event_rules import get_event_rule_action
    except ImportError:
        return False
    return get_event_rule_action(ACTION_SLUG) is not None


def _plugin_types():
    """Return the plugin object type each built-in type's references move to, by its label."""
    from core.models import ObjectType

    from ..models import CustomScript, CustomScriptProject

    models = {'customscript': CustomScript, 'customscriptproject': CustomScriptProject}
    return {label: ObjectType.objects.get_for_model(models[name]) for label, name in _TYPE_MAP.items()}


def _repoint_action(rule, entry, resolved, plugin_types, serves_action):
    """
    Point one rule's action at the plugin, writing nothing it cannot move.

    Returns whether the action needs nothing further, the count key to increment, and the refusal to
    report, so the caller owns its own counts and warnings as the other three passes do.
    """
    if entry['action_object_id'] is None or rule.action_type == ACTION_SLUG:
        return True, None, None
    if not serves_action:
        # 4.6 has no plugin action registry, so writing the slug would leave a rule nothing can
        # dispatch. Delete this branch when the floor reaches 4.7.
        refusal = (
            f'Event rule "{entry["name"]}" runs a built-in Custom Script, and this NetBox version has no '
            f'registry for plugin Event Rule actions, so it was left withdrawn. Upgrade, then repoint it.'
        )
        return False, 'unserved', refusal
    script = resolved.get(entry['action_object_id'])
    if script is None:
        refusal = (
            f'Event rule "{entry["name"]}" runs built-in Custom Script {entry["action_object_id"]}, which no '
            f'Custom Script resolves to, so it was left withdrawn.'
        )
        return False, None, refusal
    if script.is_retired:
        # The action refuses a retired script at save time, so this would raise rather than write.
        refusal = (
            f'Event rule "{entry["name"]}" resolves to {script}, which is retired and can never run again, '
            f'so the rule was left withdrawn.'
        )
        return False, None, refusal
    rule.action_type = ACTION_SLUG
    rule.action_object_type = plugin_types['extras.script']
    rule.action_object_id = script.pk
    try:
        rule.full_clean()
    except ValidationError as invalid:
        # A rule can be invalid for reasons that predate this migration, an event type its own
        # plugin stopped registering being the likely one. One rule must not end the pass.
        refusal = f'Event rule "{entry["name"]}" cannot be saved, so it was left withdrawn: {invalid.messages}'
        return False, None, refusal
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
    """Strip the built-in types from one permission and give the plugin types their own sibling."""
    from users.models import ObjectPermission

    # Keyed on the captured permission rather than on the sibling's name, because ObjectPermission
    # names are not unique: two rows sharing one would otherwise adopt each other's sibling and
    # silently drop a set of users and groups.
    split = run.journal.setdefault('split_permissions', {})
    if str(entry['pk']) in split:
        return
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
    sibling.users.set(entry['users'])
    sibling.groups.set(entry['groups'])
    split[str(entry['pk'])] = sibling.pk
    run.save(update_fields=('journal', 'last_updated'))


def _legacy_type(label):
    """Return the content type one recorded label names."""
    from django.contrib.contenttypes.models import ContentType

    app_label, model = label.split('.')
    return ContentType.objects.get(app_label=app_label, model=model)
