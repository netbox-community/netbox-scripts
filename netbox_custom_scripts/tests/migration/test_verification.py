import unittest

from django.test import TestCase

from core.models import Job
from extras.models import EventRule, Script, ScriptModule
from netbox_custom_scripts.migration import cleanup, plan, verification
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, MigrationRun
from netbox_custom_scripts.tests.migration.test_cleanup import CleanupMixin
from netbox_custom_scripts.tests.migration.test_references import HAS_EVENT_RULE_ACTIONS, REASON
from users.models import ObjectPermission


class VerificationMixin(CleanupMixin):
    """A migration taken as far as each case needs, and a way to read one check out of the report."""

    @staticmethod
    def named(report, name):
        """Return one check out of a report."""
        return next(check for check in report['checks'] if check['name'] == name)


class VerificationBeforeAnythingTestCase(VerificationMixin, TestCase):
    """What the report says when there is nothing to verify, which must not read as a pass."""

    def test_it_reports_rather_than_raising_when_no_migration_exists(self):
        MigrationRun.objects.all().delete()

        report = verification.verify()

        self.assertEqual(report['status'], plan.WARNING)
        self.assertIn('nothing to verify', str(self.named(report, verification.MODULES)['message']))

    def test_every_check_waits_for_the_step_it_verifies(self):
        # Before the cutover nothing is wrong, so nothing may report blocking.
        report = verification.verify(self.migration)

        self.assertEqual(report['status'], plan.WARNING)
        self.assertEqual({check['level'] for check in report['checks']}, {plan.WARNING})

    def test_it_reads_the_latest_run_rather_than_the_open_one(self):
        # A finished run is closed, and it is exactly the one worth verifying.
        run = self.repoint_all()
        cleanup.retire_legacy(run)
        self.assertIsNone(MigrationRun.current())

        report = verification.verify()

        self.assertNotIn('nothing to verify', str(self.named(report, verification.MODULES)['message']))


class VerificationAfterAFullRunTestCase(VerificationMixin, TestCase):
    """A migration that landed, and the proof that reading it changes nothing."""

    def setUp(self):
        super().setUp()
        # A source rule rather than an action one: moving a source needs no host registry, so the
        # green case is reachable on either line.
        self.rule = self.source_rule()

    def test_only_the_modules_check_is_short_of_ready(self):
        # Every reference has moved but the built-in modules are still there, so the report tracks
        # that rather than calling a half-finished migration done.
        run = self.repoint_all()

        report = verification.verify(run)

        self.assertEqual(len(report['checks']), 5)
        self.assertEqual(report['status'], plan.WARNING)
        modules = self.named(report, verification.MODULES)
        self.assertEqual(modules['level'], plan.WARNING)
        self.assertIn('not been retired yet', str(modules['message']))
        others = [check for check in report['checks'] if check['name'] != verification.MODULES]
        self.assertEqual({check['level'] for check in others}, {plan.READY})

    def test_each_check_names_what_it_read(self):
        run = self.repoint_all()

        report = verification.verify(run)

        for check in report['checks']:
            with self.subTest(check=check['name']):
                self.assertTrue(str(check['source']))

    def test_the_scripts_check_reads_the_built_in_rows_while_they_are_there(self):
        run = self.repoint_all()

        check = self.named(verification.verify(run), verification.SCRIPTS)

        self.assertIn('built-in', str(check['source']))

    def test_it_changes_nothing(self):
        run = self.repoint_all()
        before = self.snapshot()

        verification.verify(run)
        verification.verify(run)

        self.assertEqual(self.snapshot(), before)

    def snapshot(self):
        """Row counts plus the field values a migration moves, which is what a mutation would show."""
        return {
            'modules': ScriptModule.objects.count(),
            'scripts': Script.objects.count(),
            'jobs': Job.objects.count(),
            'rules': EventRule.objects.count(),
            'permissions': ObjectPermission.objects.count(),
            'custom_scripts': CustomScript.objects.count(),
            'projects': CustomScriptProject.objects.count(),
            'runs': MigrationRun.objects.count(),
            'serving': sorted(CustomScriptProject.objects.values_list('key', 'active_revision_id')),
            'retired': sorted(CustomScript.objects.values_list('class_name', 'is_retired')),
            'enabled_rules': sorted(EventRule.objects.values_list('pk', 'enabled')),
            'enabled_permissions': sorted(ObjectPermission.objects.values_list('pk', 'enabled')),
            'journal': MigrationRun.objects.first().journal,
            'state': MigrationRun.objects.first().state,
        }


class VerificationFailureTestCase(VerificationMixin, TestCase):
    """Each check failing for its own reason."""

    def test_a_project_serving_nothing_blocks_the_modules_check(self):
        run = self.repoint_all()
        project = CustomScriptProject.objects.filter(active_revision__isnull=False).first()
        project.active_revision = None
        project.save()

        check = self.named(verification.verify(run), verification.MODULES)

        self.assertEqual(check['level'], plan.BLOCKING)
        self.assertIn(project.key, str(check['message']))

    def test_a_retired_custom_script_blocks_the_scripts_check(self):
        run = self.repoint_all()
        script = self.plugin_script()
        script.is_retired = True
        script.save()

        check = self.named(verification.verify(run), verification.SCRIPTS)

        self.assertEqual(check['level'], plan.BLOCKING)
        self.assertIn(script.class_name, str(check['message']))

    def test_a_permission_regranted_on_the_built_in_feature_blocks_its_check(self):
        run = self.repoint_all()
        regranted = ObjectPermission.objects.create(name='regranted by hand', actions=['run'])
        regranted.object_types.add(self.script_type)

        check = self.named(verification.verify(run), verification.PERMISSIONS)

        self.assertEqual(check['level'], plan.BLOCKING)
        self.assertIn('regranted by hand', str(check['message']))

    def test_a_permission_the_repoint_left_withdrawn_warns_rather_than_blocks(self):
        # A constrained permission is left untranslated on purpose, so it still names the built-in
        # feature and always will. Reported every run, because recreating it stays outstanding.
        self.permission(constraints={'name': 'Deploy'})
        run = self.repoint_all()

        check = self.named(verification.verify(run), verification.PERMISSIONS)

        self.assertEqual(check['level'], plan.WARNING)
        self.assertIn('built-in scripts', str(check['message']))
        self.assertIn('left withdrawn', str(check['message']))

    def test_an_event_rule_created_after_the_cutover_blocks_its_check(self):
        # Not in the journal, so the migration never saw it and it is a genuine fault rather than
        # something the repoint left behind.
        run = self.repoint_all()
        self.source_rule(name='granted after the fact')

        check = self.named(verification.verify(run), verification.EVENT_RULES)

        self.assertEqual(check['level'], plan.BLOCKING)
        self.assertIn('never captured', str(check['message']))

    def test_a_job_naming_a_built_in_module_warns_rather_than_blocks(self):
        # The repoint leaves these on purpose, so the report must not read as a fault to fix.
        self.module_job(self.synced)
        run = self.repoint_all()

        check = self.named(verification.verify(run), verification.JOBS)

        self.assertEqual(check['level'], plan.WARNING)
        self.assertIn('stays where it is', str(check['message']))

    def test_a_captured_schedule_with_no_live_counterpart_warns(self):
        run = self.repoint_all()
        run.journal['schedules'] = [{'job_pk': 4242, 'name': 'nightly deploy'}]
        run.journal['recreated_schedules'] = {}
        run.save(update_fields=('journal',))

        check = self.named(verification.verify(run), verification.JOBS)

        self.assertEqual(check['level'], plan.WARNING)
        self.assertIn('nightly deploy', str(check['message']))


class VerificationAfterCleanupTestCase(VerificationMixin, TestCase):
    """The report once the built-in rows are gone, which must not pass vacuously."""

    def setUp(self):
        super().setUp()
        self.rule = self.source_rule()

    def test_it_still_passes_and_says_the_built_in_rows_are_gone(self):
        run = self.repoint_all()
        cleanup.retire_legacy(run)
        run.refresh_from_db()
        self.assertFalse(ScriptModule.objects.exists())

        report = verification.verify(run)

        self.assertEqual(report['status'], plan.READY, [str(check['message']) for check in report['checks']])
        self.assertIn('gone', str(self.named(report, verification.SCRIPTS)['message']))

    def test_the_modules_check_counts_the_projects_the_journal_recorded(self):
        run = self.repoint_all()
        cleanup.retire_legacy(run)
        run.refresh_from_db()

        check = self.named(verification.verify(run), verification.MODULES)

        self.assertEqual(check['level'], plan.READY)
        self.assertIn('2', str(check['message']))

    def test_a_project_publishing_nothing_blocks_once_the_built_in_rows_are_gone(self):
        run = self.repoint_all()
        cleanup.retire_legacy(run)
        run.refresh_from_db()
        CustomScript.objects.all().delete()

        check = self.named(verification.verify(run), verification.SCRIPTS)

        self.assertEqual(check['level'], plan.BLOCKING)
        self.assertIn('publish no Custom Script', str(check['message']))


@unittest.skipUnless(HAS_EVENT_RULE_ACTIONS, REASON)
class VerificationOfRepointedRulesTestCase(VerificationMixin, TestCase):
    """The half of the Event Rules check that only means something where the action registry exists."""

    def test_a_repointed_rule_that_is_enabled_again_passes(self):
        self.action_rule()
        run = self.repoint_all()

        check = self.named(verification.verify(run), verification.EVENT_RULES)

        self.assertEqual(check['level'], plan.READY)

    def test_a_rule_left_disabled_by_the_repoint_warns(self):
        rule = self.action_rule()
        run = self.repoint_all()
        rule.refresh_from_db()
        rule.enabled = False
        rule.save(update_fields=('enabled',))

        check = self.named(verification.verify(run), verification.EVENT_RULES)

        self.assertEqual(check['level'], plan.WARNING)
        self.assertIn(str(rule), str(check['message']))


@unittest.skipIf(HAS_EVENT_RULE_ACTIONS, 'This NetBox version can repoint an Event Rule action.')
class VerificationWithoutAnActionRegistryTestCase(VerificationMixin, TestCase):
    """Below the 4.7 line an action cannot move, and the report has to say so without crying fault."""

    def test_a_rule_the_repoint_could_not_move_warns_rather_than_blocks(self):
        # The reference pass leaves it pointing at the built-in Script on purpose and tells the
        # operator to repoint it after upgrading, so a blocking report would be permanent and wrong.
        rule = self.action_rule()
        run = self.repoint_all()

        check = self.named(verification.verify(run), verification.EVENT_RULES)

        self.assertEqual(check['level'], plan.WARNING)
        self.assertIn('could not be moved', str(check['message']))
        self.assertIn(str(rule), str(check['message']))
