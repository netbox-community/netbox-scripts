import importlib.util
import unittest
from unittest import mock

from django.test import TestCase

from core.choices import JobStatusChoices
from core.events import OBJECT_UPDATED
from core.models import ObjectType
from dcim.models import Site
from extras.models import EventRule, Script, ScriptModule, Webhook
from netbox_custom_scripts.choices import MigrationStateChoices
from netbox_custom_scripts.jobs import MigrationReferencesJob, RevisionValidationJob
from netbox_custom_scripts.migration import cutover, references
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision, MigrationRun
from netbox_custom_scripts.tests.migration.test_staging import LegacySourceMixin
from users.models import Group, ObjectPermission

# The registry arrived in NetBox 4.7, so an action can only be repointed where the host carries one.
HAS_EVENT_RULE_ACTIONS = importlib.util.find_spec('netbox.event_rules') is not None
REASON = 'This NetBox version has no Event Rule action registry.'


class ReferenceMigrationMixin(LegacySourceMixin):
    """The built-in content, the references that name it, and a migration already past activation."""

    def setUp(self):
        super().setUp()
        self.migration = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)
        self.script = Script.objects.get(module=self.synced, name='Deploy')
        self.script_type = ObjectType.objects.get_for_model(Script, for_concrete_model=False)
        self.site_type = ObjectType.objects.get_for_model(Site)

    def cross_over(self):
        """Stage, validate, cross the fence and activate, which is the state a repoint starts from."""
        for result in self.stage_all():
            revision = CustomScriptProjectRevision.objects.get(pk=result['revision_pk'])
            RevisionValidationJob.enqueue_validation(revision, immediate=True)
        cutover.enter_cutover(self.migration)
        self.migration.refresh_from_db()
        cutover.activate_staged(self.migration)
        self.migration.refresh_from_db()

    def plugin_script(self):
        """Return the Custom Script the built-in Deploy migrated to."""
        return CustomScript.objects.get(class_name='Deploy')

    def action_rule(self, name='on device change'):
        """A rule that runs the built-in Script."""
        rule = EventRule.objects.create(
            name=name,
            event_types=[OBJECT_UPDATED],
            action_type='script',
            action_object_type=self.script_type,
            action_object_id=self.script.pk,
        )
        rule.object_types.add(self.site_type)
        return rule

    def source_rule(self, name='on script change'):
        """A rule that watches the built-in Scripts and fires a webhook, so only its source moves."""
        webhook = Webhook.objects.create(name=f'notify {name}', payload_url='http://localhost/hook')
        rule = EventRule.objects.create(
            name=name,
            event_types=[OBJECT_UPDATED],
            action_type='webhook',
            action_object_type=ObjectType.objects.get_for_model(Webhook),
            action_object_id=webhook.pk,
        )
        rule.object_types.add(self.script_type)
        return rule

    def permission(self, *, actions=('view', 'run'), constraints=None, extra_type=None, types=None):
        permission = ObjectPermission.objects.create(
            name='built-in scripts',
            description='granted before the migration',
            actions=list(actions),
            constraints=constraints,
        )
        permission.object_types.set([item.pk for item in (types or [self.script_type])])
        if extra_type is not None:
            permission.object_types.add(extra_type.pk)
        return permission


@unittest.skipUnless(HAS_EVENT_RULE_ACTIONS, REASON)
class RepointEventRulesTestCase(ReferenceMigrationMixin, TestCase):
    """Moving an Event Rule's action and its event sources onto the plugin."""

    def test_a_rule_that_runs_a_built_in_script_runs_the_custom_script_instead(self):
        rule = self.action_rule()
        self.cross_over()

        counts, warnings = references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        self.assertEqual(rule.action_type, references.ACTION_SLUG)
        self.assertEqual(rule.action_object_id, self.plugin_script().pk)
        # By key, because ObjectType is multi-table inheritance rather than a proxy, so the
        # ContentType this field returns never compares equal to the ObjectType sharing its key.
        self.assertEqual(rule.action_object_type_id, ObjectType.objects.get_for_model(CustomScript).pk)
        self.assertEqual(counts['actions'], 1)
        self.assertEqual(warnings, [])

    def test_the_repointed_action_is_one_the_host_can_dispatch(self):
        # The whole point of the move: a rule nothing serves is a rule that silently never fires.
        rule = self.action_rule()
        self.cross_over()

        references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        self.assertTrue(rule.action_is_available)

    def test_a_rule_watching_the_built_in_scripts_watches_the_custom_scripts_instead(self):
        rule = self.source_rule()
        self.cross_over()

        counts, _warnings = references.repoint_event_rules(self.migration)

        types = set(rule.object_types.values_list('pk', flat=True))
        self.assertIn(ObjectType.objects.get_for_model(CustomScript).pk, types)
        self.assertNotIn(self.script_type.pk, types)
        # Its action was never legacy, so it is left exactly as it was.
        self.assertEqual(rule.action_type, 'webhook')
        self.assertEqual(counts['sources'], 1)
        self.assertEqual(counts['actions'], 0)

    def test_a_rule_needing_both_changes_gets_both(self):
        rule = self.action_rule()
        rule.object_types.add(self.script_type.pk)
        self.cross_over()

        counts, _warnings = references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        types = set(rule.object_types.values_list('pk', flat=True))
        self.assertEqual(rule.action_type, references.ACTION_SLUG)
        self.assertIn(ObjectType.objects.get_for_model(CustomScript).pk, types)
        self.assertNotIn(self.script_type.pk, types)
        # The unrelated type it also watched is untouched.
        self.assertIn(self.site_type.pk, types)
        self.assertEqual(counts, {'actions': 1, 'sources': 1, 'restored': 1, 'unmovable': 0})

    def test_a_rule_goes_back_into_service_in_the_state_it_was_captured_in(self):
        enabled = self.action_rule('enabled rule')
        disabled = self.action_rule('disabled rule')
        disabled.enabled = False
        disabled.save(update_fields=('enabled',))
        self.cross_over()
        # The fence withdrew both, so neither is firing when the repoint starts.
        self.assertEqual(EventRule.objects.filter(enabled=True).count(), 0)

        counts, _warnings = references.repoint_event_rules(self.migration)

        enabled.refresh_from_db()
        disabled.refresh_from_db()
        self.assertTrue(enabled.enabled)
        self.assertFalse(disabled.enabled)
        self.assertEqual(counts['restored'], 1)

    def test_a_rule_whose_script_does_not_resolve_stays_withdrawn(self):
        rule = self.action_rule()
        self.cross_over()
        # What a project that never activated, or a class that stopped publishing, looks like.
        self.plugin_script().delete()

        counts, warnings = references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.assertEqual(rule.action_type, 'script')
        self.assertEqual(counts['actions'], 0)
        self.assertTrue(any(rule.name in warning for warning in warnings))

    def test_a_rule_resolving_to_a_retired_script_stays_withdrawn(self):
        # The action refuses a retired script at save time, so writing it would raise instead.
        rule = self.action_rule()
        self.cross_over()
        CustomScript.objects.filter(pk=self.plugin_script().pk).update(is_retired=True)

        counts, warnings = references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        self.assertFalse(rule.enabled)
        self.assertEqual(counts['actions'], 0)
        self.assertTrue(any('retired' in warning for warning in warnings))

    def test_a_rule_that_was_already_invalid_is_reported_rather_than_ending_the_pass(self):
        # Its own plugin could have stopped registering an event type long before this migration.
        broken = self.action_rule('broken rule')
        healthy = self.action_rule('healthy rule')
        self.cross_over()
        EventRule.objects.filter(pk=broken.pk).update(conditions={'nonsense': True})

        counts, warnings = references.repoint_event_rules(self.migration)

        broken.refresh_from_db()
        healthy.refresh_from_db()
        self.assertEqual(broken.action_type, 'script')
        self.assertFalse(broken.enabled)
        self.assertTrue(any(broken.name in warning for warning in warnings))
        # The healthy sibling still moved, which is the whole point of reporting rather than raising.
        self.assertEqual(healthy.action_type, references.ACTION_SLUG)
        self.assertEqual(counts['actions'], 1)

    def test_a_second_pass_returns_the_recorded_counts_and_changes_nothing(self):
        rule = self.action_rule()
        self.cross_over()
        first, _warnings = references.repoint_event_rules(self.migration)
        self.migration.refresh_from_db()
        rule.refresh_from_db()
        rule.enabled = False
        rule.save(update_fields=('enabled',))

        second, warnings = references.repoint_event_rules(self.migration)

        self.assertEqual(second, first)
        self.assertEqual(warnings, [])
        rule.refresh_from_db()
        # An operator turning a rule off after the migration must stay turned off.
        self.assertFalse(rule.enabled)

    def test_the_probe_finds_the_action_this_plugin_registered(self):
        self.assertTrue(references.host_serves_action())


class UnservedActionTestCase(ReferenceMigrationMixin, TestCase):
    """What a host with no plugin Event Rule action registry gets, which is every 4.6 release."""

    def test_the_source_half_moves_and_the_action_half_is_reported(self):
        rule = self.action_rule()
        rule.object_types.add(self.script_type.pk)
        self.cross_over()

        with mock.patch.object(references, 'host_serves_action', return_value=False):
            counts, warnings = references.repoint_event_rules(self.migration)

        rule.refresh_from_db()
        types = set(rule.object_types.values_list('pk', flat=True))
        # The half that needs no registry still moves.
        self.assertIn(ObjectType.objects.get_for_model(CustomScript).pk, types)
        self.assertNotIn(self.script_type.pk, types)
        self.assertEqual(counts['sources'], 1)
        # The half that does is left alone and named, and the rule stays withdrawn.
        self.assertEqual(rule.action_type, 'script')
        self.assertEqual(rule.action_object_id, self.script.pk)
        self.assertFalse(rule.enabled)
        self.assertEqual(counts, {'actions': 0, 'sources': 1, 'restored': 0, 'unmovable': 1})
        self.assertTrue(any(rule.name in warning and 'Upgrade' in warning for warning in warnings))


class RepointPermissionsTestCase(ReferenceMigrationMixin, TestCase):
    """Moving a grant on the built-in feature onto the plugin's own object types."""

    def test_a_permission_naming_only_the_built_in_feature_is_swapped_in_place(self):
        group = Group.objects.create(name='operators')
        permission = self.permission(actions=('view', 'run'))
        permission.groups.add(group)
        self.cross_over()

        counts, warnings = references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        self.assertEqual(
            set(permission.object_types.values_list('pk', flat=True)),
            {ObjectType.objects.get_for_model(CustomScript).pk},
        )
        self.assertEqual(permission.actions, ['view', 'run'])
        self.assertTrue(permission.enabled)
        # Who held it is untouched, because the row itself is what moved.
        self.assertEqual([item.name for item in permission.groups.all()], ['operators'])
        self.assertEqual(counts, {'swapped': 1, 'split': 0, 'constrained': 0, 'unmappable': 0})
        self.assertEqual(warnings, [])

    def test_a_permission_on_the_built_in_modules_moves_onto_the_projects(self):
        module_type = ObjectType.objects.get_for_model(ScriptModule, for_concrete_model=False)
        permission = self.permission(actions=('view', 'add', 'change', 'delete'), types=[module_type])
        self.cross_over()

        references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        self.assertEqual(
            set(permission.object_types.values_list('pk', flat=True)),
            {ObjectType.objects.get_for_model(CustomScriptProject).pk},
        )
        self.assertEqual(permission.actions, ['view', 'add', 'change', 'delete'])

    def test_a_permission_naming_unrelated_types_keeps_them_and_gains_a_sibling(self):
        group = Group.objects.create(name='operators')
        permission = self.permission(actions=('view', 'run'), extra_type=self.site_type)
        permission.groups.add(group)
        self.cross_over()

        counts, _warnings = references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        # The original keeps what was never part of this migration, and goes back into service.
        self.assertEqual(set(permission.object_types.values_list('pk', flat=True)), {self.site_type.pk})
        self.assertEqual(permission.actions, ['view', 'run'])
        self.assertTrue(permission.enabled)
        sibling = ObjectPermission.objects.get(name=f'built-in scripts{references._SIBLING_SUFFIX}')
        self.assertEqual(
            set(sibling.object_types.values_list('pk', flat=True)),
            {ObjectType.objects.get_for_model(CustomScript).pk},
        )
        self.assertEqual(sibling.actions, ['view', 'run'])
        self.assertTrue(sibling.enabled)
        self.assertEqual([item.name for item in sibling.groups.all()], ['operators'])
        self.assertEqual(counts, {'swapped': 0, 'split': 1, 'constrained': 0, 'unmappable': 0})

    def test_a_group_deleted_during_the_window_is_reported_rather_than_fatal(self):
        # The stale key inserts fine, then the deferred FK fails the pass at commit and every re-run.
        group = Group.objects.create(name='operators')
        permission = self.permission(actions=('view', 'run'), extra_type=self.site_type)
        permission.groups.add(group)
        self.cross_over()
        group.delete()

        counts, warnings = references.repoint_permissions(self.migration)

        self.assertEqual(counts['split'], 1)
        sibling = ObjectPermission.objects.get(name=f'built-in scripts{references._SIBLING_SUFFIX}')
        self.assertEqual(list(sibling.groups.all()), [])
        self.assertTrue(any('no longer exist' in warning for warning in warnings))

    def test_a_constrained_permission_is_reported_and_left_untouched(self):
        # Its filters name fields the plugin models do not have, so neither copying nor dropping
        # them is safe. It stays withdrawn and an operator is told about it by name.
        permission = self.permission(constraints={'name__startswith': 'Deploy'})
        self.cross_over()

        counts, warnings = references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        self.assertEqual(set(permission.object_types.values_list('pk', flat=True)), {self.script_type.pk})
        self.assertFalse(permission.enabled)
        self.assertEqual(counts['constrained'], 1)
        self.assertEqual(counts['swapped'], 0)
        self.assertTrue(any(permission.name in warning for warning in warnings))

    def test_a_permission_with_nothing_left_to_grant_is_left_withdrawn(self):
        # Swapping it would leave an enabled permission granting nothing, so it is treated the way
        # a constrained one is: withdrawn and named.
        permission = self.permission(actions=('add', 'delete'))
        self.cross_over()

        counts, warnings = references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        self.assertEqual(set(permission.object_types.values_list('pk', flat=True)), {self.script_type.pk})
        self.assertFalse(permission.enabled)
        self.assertEqual(permission.actions, ['add', 'delete'])
        self.assertEqual(counts['unmappable'], 1)
        self.assertEqual(counts['swapped'], 0)
        self.assertTrue(any('none of which' in warning for warning in warnings))

    def test_two_permissions_sharing_a_name_each_get_their_own_sibling(self):
        # ObjectPermission.name is not unique, so keying the sibling on it would make the second
        # permission adopt the first's and silently drop its own users and groups.
        first = self.permission(extra_type=self.site_type)
        second = self.permission(extra_type=self.site_type)
        group = Group.objects.create(name='second holders')
        second.groups.add(group)
        self.cross_over()

        counts, _warnings = references.repoint_permissions(self.migration)

        self.assertEqual(counts['split'], 2)
        siblings = ObjectPermission.objects.filter(name__endswith=references._SIBLING_SUFFIX)
        self.assertEqual(siblings.count(), 2)
        # The second permission's own holders reached its own sibling.
        self.assertEqual(sorted(item.name for sibling in siblings for item in sibling.groups.all()), ['second holders'])
        self.migration.refresh_from_db()
        self.assertEqual(sorted(self.migration.journal['split_permissions']), sorted([str(first.pk), str(second.pk)]))

    def test_an_action_the_plugin_does_not_separate_is_reported_rather_than_granted(self):
        permission = self.permission(actions=('view', 'add', 'delete'))
        self.cross_over()

        counts, warnings = references.repoint_permissions(self.migration)

        permission.refresh_from_db()
        # A derived row is never created or deleted by hand, so neither action has a counterpart.
        self.assertEqual(permission.actions, ['view'])
        self.assertEqual(counts['swapped'], 1)
        self.assertTrue(any('add' in warning and 'delete' in warning for warning in warnings))

    def test_a_second_pass_returns_the_recorded_counts_and_creates_no_second_sibling(self):
        self.permission(extra_type=self.site_type)
        self.cross_over()
        first, _warnings = references.repoint_permissions(self.migration)
        self.migration.refresh_from_db()

        second, _warnings = references.repoint_permissions(self.migration)

        self.assertEqual(second, first)
        self.assertEqual(ObjectPermission.objects.filter(name__endswith=references._SIBLING_SUFFIX).count(), 1)


class ReferencePassOrderingTestCase(ReferenceMigrationMixin, TestCase):
    """The pass refuses until the rows it points at exist."""

    def test_it_refuses_before_the_fence(self):
        with self.assertRaises(cutover.CutoverRefused):
            references.repoint_event_rules(self.migration)

    def test_it_refuses_after_the_fence_but_before_activation(self):
        for result in self.stage_all():
            revision = CustomScriptProjectRevision.objects.get(pk=result['revision_pk'])
            RevisionValidationJob.enqueue_validation(revision, immediate=True)
        cutover.enter_cutover(self.migration)
        self.migration.refresh_from_db()

        with self.assertRaises(cutover.CutoverRefused) as caught:
            references.repoint_permissions(self.migration)

        self.assertIn('not been activated', str(caught.exception))

    def test_it_refuses_when_no_migration_is_open(self):
        with self.assertRaises(cutover.CutoverRefused):
            references.repoint_event_rules(None)


class MigrationReferencesJobTestCase(ReferenceMigrationMixin, TestCase):
    """The job both halves are started from."""

    def test_the_job_reports_what_it_moved(self):
        self.action_rule()
        self.permission()
        self.cross_over()

        job = MigrationReferencesJob.enqueue(immediate=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        messages = ' '.join(entry['message'] for entry in job.log_entries)
        self.assertIn('Event Rule action(s)', messages)
        self.assertIn('Moved 1 permission(s)', messages)
        # One button does all four repoints, so the job speaks for every one of them.
        self.assertIn('Job(s) of history', messages)
        self.assertIn('Recreated 0 schedule(s)', messages)

    def test_the_job_records_all_four_steps_so_a_re_run_repeats_none(self):
        self.action_rule()
        self.cross_over()

        MigrationReferencesJob.enqueue(immediate=True)

        self.migration.refresh_from_db()
        for step in (
            references.EVENT_RULES_STEP,
            references.PERMISSIONS_STEP,
            references.HISTORY_STEP,
            references.SCHEDULES_STEP,
        ):
            with self.subTest(step=step):
                self.assertTrue(self.migration.step_done(step))

    def test_the_job_logs_every_warning_it_raised(self):
        permission = self.permission(constraints={'name': 'Deploy'})
        self.cross_over()

        job = MigrationReferencesJob.enqueue(immediate=True)

        job.refresh_from_db()
        warnings = [entry['message'] for entry in job.log_entries if entry['level'] == 'warning']
        self.assertTrue(any(permission.name in message for message in warnings))

    def test_the_job_fails_before_activation(self):
        job = MigrationReferencesJob.enqueue(immediate=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertIn('cutover has not been entered', ' '.join(e['message'] for e in job.log_entries))
