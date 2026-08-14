"""Moving the references an installation holds off the built-in Custom Scripts onto plugin rows."""

from django.core.exceptions import ValidationError
from django.db import transaction

from . import cutover, mapping

__all__ = (
    'ACTION_SLUG',
    'EVENT_RULES_STEP',
    'PERMISSIONS_STEP',
    'repoint_event_rules',
    'repoint_permissions',
)

# Declared here rather than imported from event_rules.py, which imports netbox.event_rules at module
# level. That module is 4.7 only, so importing it would take this whole tier down on a 4.6 host.
ACTION_SLUG = 'netbox_custom_scripts.run'

EVENT_RULES_STEP = 'repoint_event_rules'
PERMISSIONS_STEP = 'repoint_permissions'

# Which plugin model each built-in one's references move to.
_TYPE_MAP = {
    'extras.script': 'customscript',
    'extras.scriptmodule': 'customscriptproject',
}

# Which granted actions have a counterpart, per built-in type. The codenames are identical on both
# sides, because phase 1 froze them for this move, so there is nothing to rename. An action absent
# here is reported rather than approximated.
_ACTIONS = {
    'extras.script': frozenset({'view', 'change', 'run'}),
    'extras.scriptmodule': frozenset({'view', 'add', 'change', 'delete'}),
}

# Appended to the name of the permission created when a mixed one is split.
_SIBLING_SUFFIX = ' (Custom Scripts)'


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
        return run.journal['steps'][EVENT_RULES_STEP].get('counts', {}), []

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
            moved = _repoint_action(rule, entry, resolved, plugin_types, serves_action, counts, warnings)
            _repoint_sources(rule, entry, plugin_types, counts)
            # Only a rule with nothing left pointing at the built-in feature goes back into service.
            if moved and entry['enabled'] and not rule.enabled:
                rule.enabled = True
                rule.save(update_fields=('enabled',))
                counts['restored'] += 1
    _record(run, EVENT_RULES_STEP, counts, warnings)
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
        return run.journal['steps'][PERMISSIONS_STEP].get('counts', {}), []

    plugin_types = _plugin_types()
    counts = {'swapped': 0, 'split': 0, 'constrained': 0}
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
        if unmappable:
            warnings.append(
                f'Permission "{entry["name"]}" granted {", ".join(sorted(unmappable))} on the built-in Custom '
                f'Scripts, which the plugin does not separate, so that grant was not preserved.'
            )
        targets = [plugin_types[label].pk for label in entry['legacy_object_types'] if label in plugin_types]
        with transaction.atomic():
            if entry['other_object_types']:
                _split_permission(permission, entry, targets, actions)
                counts['split'] += 1
            else:
                _swap_permission(permission, entry, targets, actions)
                counts['swapped'] += 1
    _record(run, PERMISSIONS_STEP, counts, warnings)
    return counts, warnings


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


def _repoint_action(rule, entry, resolved, plugin_types, serves_action, counts, warnings):
    """Point one rule's action at the plugin, reporting rather than writing what cannot move."""
    if entry['action_object_id'] is None:
        return True
    if rule.action_type == ACTION_SLUG:
        return True
    if not serves_action:
        # 4.6 has no plugin action registry, so writing the slug would leave a rule nothing can
        # dispatch. Delete this branch when the floor reaches 4.7.
        counts['unserved'] += 1
        warnings.append(
            f'Event rule "{entry["name"]}" runs a built-in Custom Script, and this NetBox version has no '
            f'registry for plugin Event Rule actions, so it was left withdrawn. Upgrade, then repoint it.'
        )
        return False
    script = resolved.get(entry['action_object_id'])
    if script is None:
        warnings.append(
            f'Event rule "{entry["name"]}" runs built-in Custom Script {entry["action_object_id"]}, which no '
            f'Custom Script resolves to, so it was left withdrawn.'
        )
        return False
    if script.is_retired:
        # The action refuses a retired script at save time, so this would raise rather than write.
        warnings.append(
            f'Event rule "{entry["name"]}" resolves to {script}, which is retired and can never run again, '
            f'so the rule was left withdrawn.'
        )
        return False
    rule.action_type = ACTION_SLUG
    rule.action_object_type = plugin_types['extras.script']
    rule.action_object_id = script.pk
    try:
        rule.full_clean()
    except ValidationError as invalid:
        # A rule can be invalid for reasons that predate this migration, an event type its own
        # plugin stopped registering being the likely one. One rule must not end the pass.
        warnings.append(f'Event rule "{entry["name"]}" cannot be saved, so it was left withdrawn: {invalid.messages}')
        return False
    rule.save(update_fields=('action_type', 'action_object_type', 'action_object_id'))
    counts['actions'] += 1
    return True


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


def _split_permission(permission, entry, targets, actions):
    """Strip the built-in types from one permission and give the plugin types their own sibling."""
    from users.models import ObjectPermission

    legacy = [_legacy_type(label).pk for label in entry['legacy_object_types']]
    permission.object_types.remove(*legacy)
    # Its remaining types were never part of this migration, so the fence's withdrawal is undone.
    permission.enabled = entry['enabled']
    permission.save(update_fields=('enabled',))
    name = f'{permission.name}{_SIBLING_SUFFIX}'
    sibling, created = ObjectPermission.objects.get_or_create(
        # Truncated because the field is bounded and a long original would otherwise raise.
        name=name[: ObjectPermission._meta.get_field('name').max_length],
        defaults={'description': entry['description'], 'enabled': entry['enabled'], 'actions': actions},
    )
    if not created:
        return
    sibling.object_types.set(targets)
    sibling.users.set(entry['users'])
    sibling.groups.set(entry['groups'])


def _legacy_type(label):
    """Return the content type one recorded label names."""
    from django.contrib.contenttypes.models import ContentType

    app_label, model = label.split('.')
    return ContentType.objects.get(app_label=app_label, model=model)


def _record(run, step, counts, warnings):
    """Persist one step's warnings on the run, then mark it complete."""
    if warnings:
        run.warnings = [*run.warnings, *warnings]
        run.save(update_fields=('warnings', 'last_updated'))
    run.record_step(step, counts=counts)
