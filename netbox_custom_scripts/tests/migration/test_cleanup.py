import uuid

from django.core.files.storage import storages
from django.test import TestCase

from core.models import Job, ObjectType
from extras.models import EventRule, Script, ScriptModule
from netbox_custom_scripts.choices import MigrationStateChoices
from netbox_custom_scripts.migration import cleanup, cutover, references
from netbox_custom_scripts.models import CustomScriptProject, MigrationRun
from netbox_custom_scripts.tests.migration.test_references import ReferenceMigrationMixin
from netbox_custom_scripts.tests.migration.test_staging import LEGACY_SCRIPT


class CleanupMixin(ReferenceMigrationMixin):
    """A migration that has crossed the fence, activated, and moved its Job history."""

    def repointed(self):
        """Take the migration to the state cleanup is the next step from, every reference step done."""
        return self.repoint_all()

    def repoint_all(self):
        """Run every reference pass in the order the job runs them."""
        self.cross_over()
        references.repoint_event_rules(self.migration)
        references.repoint_permissions(self.migration)
        references.repoint_job_history(self.migration)
        references.recreate_schedules(self.migration)
        self.migration.refresh_from_db()
        return self.migration

    def module_type(self):
        return ObjectType.objects.get_for_model(ScriptModule, for_concrete_model=False)

    def module_job(self, module):
        """One Job naming a built-in script module, which older NetBox versions created."""
        return Job.objects.create(
            name='sync',
            object_type=self.module_type(),
            object_id=module.pk,
            job_id=uuid.uuid4(),
            queue_name='default',
        )

    def blocked_module(self, module):
        """Hold one module back with something an operator can clear: live, unresolved history."""
        stranded = Script.objects.create(module=module, name='Removed')
        return self.script_job(stranded)

    def script_job(self, script):
        """One Job naming a built-in Script, as a run before the migration left behind."""
        return Job.objects.create(
            name=script.name,
            object_type=self.script_type,
            object_id=script.pk,
            job_id=uuid.uuid4(),
            queue_name='default',
        )


class CleanupRefusalTestCase(CleanupMixin, TestCase):
    """What cleanup refuses, and why each refusal exists."""

    def test_it_refuses_when_no_migration_is_open(self):
        with self.assertRaises(cutover.CutoverRefused):
            cleanup.retire_legacy(None)

    def test_it_refuses_before_the_fence_has_been_recorded(self):
        with self.assertRaises(cutover.CutoverRefused):
            cleanup.retire_legacy(self.migration)

    def test_it_refuses_before_the_job_history_has_moved(self):
        # Deleting a Script takes its Job rows with it, so this ordering is the whole guarantee.
        self.cross_over()

        with self.assertRaises(cutover.CutoverRefused) as refused:
            cleanup.retire_legacy(self.migration)

        self.assertIn('Job history', str(refused.exception))

    def test_it_refuses_when_the_schedules_step_has_not_finished(self):
        # A schedule is recreated through the rows this pass deletes, so deleting first loses it.
        self.cross_over()
        references.repoint_event_rules(self.migration)
        references.repoint_permissions(self.migration)
        references.repoint_job_history(self.migration)
        self.migration.refresh_from_db()

        with self.assertRaises(cutover.CutoverRefused) as refused:
            cleanup.retire_legacy(self.migration)

        self.assertIn(references.SCHEDULES_STEP, str(refused.exception))

    def test_it_is_not_ready_until_every_reference_step_is_done(self):
        self.cross_over()
        self.assertFalse(cleanup.ready(self.migration))

        run = self.repoint_all()

        self.assertTrue(cleanup.ready(run))

    def test_a_refusal_deletes_nothing(self):
        self.cross_over()

        with self.assertRaises(cutover.CutoverRefused):
            cleanup.retire_legacy(self.migration)

        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(ScriptModule.objects.filter(pk=self.uploaded.pk).exists())


class CleanupDeletionTestCase(CleanupMixin, TestCase):
    """What a complete pass deletes, and what it deliberately leaves alone."""

    def test_it_deletes_every_mapped_module_and_its_scripts(self):
        run = self.repointed()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['modules'], 2)
        self.assertEqual(counts['blocked'], 0)
        self.assertEqual(counts['retained'], 0)
        self.assertEqual(warnings, [])
        self.assertFalse(ScriptModule.objects.exists())
        self.assertFalse(Script.objects.exists())

    def test_the_stored_source_is_deleted_with_the_module(self):
        # Proves the instance path ran: QuerySet.delete() skips the delete() that removes the file.
        run = self.repointed()
        path = self.uploaded.file_path
        self.assertTrue(storages['scripts'].exists(path))

        cleanup.retire_legacy(run)

        self.assertFalse(storages['scripts'].exists(path))

    def test_it_reaches_the_migrated_state_and_records_when(self):
        run = self.repointed()

        cleanup.retire_legacy(run)

        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)
        self.assertIsNotNone(run.completed)
        self.assertTrue(run.step_done(cleanup.STEP))

    def test_a_second_pass_changes_nothing_and_returns_what_the_first_recorded(self):
        run = self.repointed()
        first, _unused = cleanup.retire_legacy(run)
        run.refresh_from_db()

        second, warnings = cleanup.retire_legacy(run)

        self.assertEqual(second, first)
        self.assertEqual(warnings, [])

    def test_a_module_this_migration_never_staged_is_left_alone(self):
        # The scope is the map the fence froze, so a module added afterwards is not in it.
        run = self.repointed()
        other = self.legacy_uploaded_module('unrelated.py', b'x = 1\n')

        counts, _unused = cleanup.retire_legacy(run)

        self.assertTrue(ScriptModule.objects.filter(pk=other.pk).exists())
        self.assertEqual(counts['unserved'], 0)

    def test_a_report_rooted_module_is_left_alone(self):
        # The built-in manager admits reports as well, and a migration never staged one.
        run = self.repointed()
        report = self.legacy_uploaded_module('audit.py', b'x = 1\n')
        ScriptModule.objects.filter(pk=report.pk).update(file_root='reports')

        cleanup.retire_legacy(run)

        self.assertTrue(ScriptModule.objects.filter(pk=report.pk).exists())

    def test_a_module_whose_project_serves_nothing_is_left_in_place(self):
        run = self.repointed()
        project = CustomScriptProject.objects.get(key__startswith='automation')
        project.active_revision = None
        project.save()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['unserved'], 1)
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(any('not' in warning and 'serving' in warning for warning in warnings))
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.CUTOVER)

    def test_a_deleted_project_is_refused_per_module_rather_than_counted_unserved(self):
        # One migrated Project is gone, so nothing resolves the built-in classes it replaced.
        run = self.repointed()
        CustomScriptProject.objects.get(key__startswith='automation').delete()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['unserved'], 0)
        self.assertEqual(counts['blocked'], 1)
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(any('resolves to' in warning for warning in warnings))
        # The Project that is still serving is unaffected by its neighbour going away.
        self.assertFalse(ScriptModule.objects.filter(pk=self.uploaded.pk).exists())

    def test_the_plugin_still_serves_what_the_built_in_feature_did(self):
        run = self.repointed()
        script = self.plugin_script()

        cleanup.retire_legacy(run)

        script.refresh_from_db()
        self.assertFalse(script.is_retired)
        self.assertIsNotNone(script.project.active_revision)

    def test_the_sweep_reports_what_still_names_the_built_in_feature(self):
        run = self.repointed()

        counts, _unused = cleanup.retire_legacy(run)

        self.assertEqual(counts['jobs'], 0)
        self.assertIn('event_rules', counts)
        self.assertIn('permissions', counts)


class CleanupHistoryGuardTestCase(CleanupMixin, TestCase):
    """The per-module refusal, which is the safety net the whole ordering exists for."""

    def test_a_module_whose_live_script_holds_history_blocks_and_its_sibling_is_deleted(self):
        run = self.repointed()
        stranded = Script.objects.create(module=self.synced, name='Removed')
        job = self.script_job(stranded)

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 1)
        self.assertEqual(counts['modules'], 1)
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertFalse(ScriptModule.objects.filter(pk=self.uploaded.pk).exists())
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())
        self.assertTrue(any('Job history' in warning for warning in warnings))

    def test_a_module_holding_its_own_job_history_is_retained_and_the_run_still_closes(self):
        # No plugin row can hold these, and JobsMixin.delete() would take them without the guard.
        run = self.repointed()
        job = self.module_job(self.synced)

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['retained'], 1)
        self.assertEqual(counts['blocked'], 0)
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(any(self.synced.python_name in warning for warning in warnings))
        # Nothing an operator does clears this, so holding the run open would strand it forever.
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)

    def test_a_soft_deleted_script_does_not_hold_the_run_open(self):
        # The normal state of a long-lived installation, and nothing can take that history over.
        retired = Script.objects.create(module=self.synced, name='OldDeploy')
        Script.objects.filter(pk=retired.pk).update(is_executable=False)
        job = self.script_job(retired)
        run = self.repointed()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['retained'], 1)
        self.assertEqual(counts['blocked'], 0)
        self.assertTrue(Job.objects.filter(pk=job.pk).exists())
        self.assertTrue(any('left the file' in warning for warning in warnings))
        # The reference pass has to have closed too, or this could never have been reached.
        self.assertTrue(run.step_done(references.HISTORY_STEP))
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)

    def test_a_module_publishing_an_unresolved_class_is_not_deleted(self):
        # Deleting it would leave a script that used to run unable to run at all.
        run = self.repointed()
        self.plugin_script().delete()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 1)
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(any('resolves to' in warning for warning in warnings))
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.CUTOVER)

    def test_a_blocked_pass_stays_resumable(self):
        run = self.repointed()
        job = self.blocked_module(self.synced)

        cleanup.retire_legacy(run)

        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.CUTOVER)
        self.assertFalse(run.step_done(cleanup.STEP))
        self.assertEqual(MigrationRun.current(), run)

        job.delete()
        counts, _unused = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 0)
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)

    def test_a_partial_pass_over_nested_folders_still_sees_what_it_skipped(self):
        # Deleting the shallower module changes what the deeper one would group into.
        deep = self.legacy_synced_module('automation/deep/b.py', LEGACY_SCRIPT)
        run = self.repointed()
        held = self.blocked_module(deep)

        first, _unused = cleanup.retire_legacy(run)
        self.assertEqual(first['blocked'], 1)
        self.assertTrue(ScriptModule.objects.filter(pk=deep.pk).exists())
        self.assertFalse(ScriptModule.objects.filter(pk=self.synced.pk).exists())

        held.delete()
        second, _unused = cleanup.retire_legacy(run)

        self.assertEqual(second['unserved'], 0)
        self.assertEqual(second['modules'], 1)
        self.assertFalse(ScriptModule.objects.filter(pk=deep.pk).exists())
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)

    def test_a_stranded_script_holding_history_is_named_by_what_the_operator_can_fix(self):
        # Both refusals apply to this module. The one naming an action has to win, or the advice
        # sends the operator to a pass that has nothing left to do.
        run = self.repointed()
        self.script_job(self.script)
        self.plugin_script().delete()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 1)
        self.assertTrue(any('stage it again' in str(warning) for warning in warnings))
        self.assertFalse(any('Run that pass again' in str(warning) for warning in warnings))

    def test_a_helper_only_module_does_not_hold_the_migration_open(self):
        # Its Project declares nothing, so the revision is vacuously valid and can be activated,
        # which is what lets this module be deleted like any other.
        helper = self.legacy_synced_module('shared/util.py', b'def describe():\n    return 1\n')
        run = self.repointed()

        counts, _warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['unserved'], 0)
        self.assertFalse(ScriptModule.objects.filter(pk=helper.pk).exists())
        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)

    def test_the_stored_source_of_a_skipped_module_survives(self):
        run = self.repointed()
        self.blocked_module(self.uploaded)
        path = self.uploaded.file_path

        cleanup.retire_legacy(run)

        self.assertTrue(storages['scripts'].exists(path))


class LegacyFeatureStateTestCase(CleanupMixin, TestCase):
    """
    What an installation is left holding, read without any plugin code in the path.

    This is the honest form of "disabling the plugin does not reactivate the built-in feature": every
    closure the migration makes is a row in a core table, so the guarantee is whatever those rows say.
    """

    def test_every_closure_is_a_core_row_that_survives_the_plugin(self):
        # A source rule: moving one needs no host registry, and this has to hold on either line.
        permission = self.permission()
        rule = self.source_rule()
        run = self.repointed()
        references.repoint_event_rules(run)
        references.repoint_permissions(run)
        run.refresh_from_db()

        cleanup.retire_legacy(run)

        # Nothing left to discover, and nothing left to run.
        self.assertFalse(ScriptModule.objects.exists())
        self.assertFalse(Script.objects.exists())
        # Rows core reads on its own. What closes the feature is that none of them still names it.
        permission.refresh_from_db()
        self.assertNotIn(self.script_type.pk, permission.object_types.values_list('pk', flat=True))
        self.assertNotIn(self.script_type.pk, rule.object_types.values_list('pk', flat=True))

    def test_the_projects_the_migration_created_are_what_remains(self):
        run = self.repointed()

        cleanup.retire_legacy(run)

        self.assertTrue(CustomScriptProject.objects.exists())
        for project in CustomScriptProject.objects.all():
            self.assertIsNotNone(project.active_revision)


class CleanupAfterActionRepointTestCase(CleanupMixin, TestCase):
    """What cleanup does once an Event Rule's action has actually moved, which needs the registry."""

    def test_a_repointed_action_does_not_hold_its_module_back(self):
        # The rule has to exist before the fence, because the reference pass replays only the
        # journal the cutover froze.
        rule = self.action_rule()
        run = self.repointed()

        counts, _unused = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 0)
        self.assertTrue(EventRule.objects.filter(pk=rule.pk).exists())
        self.assertFalse(ScriptModule.objects.filter(pk=self.synced.pk).exists())

    def test_a_rule_that_appeared_after_the_fence_holds_its_module_back(self):
        # Uncaptured, so no pass moved it, and on this host one still could: a fault an operator
        # clears with a fresh run rather than permanent residue.
        run = self.repointed()
        rule = self.action_rule()

        counts, warnings = cleanup.retire_legacy(run)

        self.assertEqual(counts['blocked'], 1)
        self.assertEqual(counts['retained'], 0)
        self.assertTrue(EventRule.objects.filter(pk=rule.pk).exists())
        self.assertTrue(ScriptModule.objects.filter(pk=self.synced.pk).exists())
        self.assertTrue(any('did not' in warning and 'move' in warning for warning in warnings))
        run.refresh_from_db()
        self.assertNotEqual(run.state, MigrationStateChoices.MIGRATED)
