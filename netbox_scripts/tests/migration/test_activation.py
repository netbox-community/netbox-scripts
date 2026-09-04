from django.test import TestCase

from core.choices import JobStatusChoices
from extras.models import ScriptModule
from netbox_scripts.choices import MigrationStateChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.jobs import MigrationActivationJob
from netbox_scripts.migration import cutover, mapping
from netbox_scripts.models import CustomScript, MigrationRun, ScriptProject, ScriptProjectRevision
from netbox_scripts.tests.migration.test_staging import LegacySourceMixin


class ActivateStagedTestCase(LegacySourceMixin, TestCase):
    """Putting the staged Projects into service, end to end from the built-in rows."""

    def setUp(self):
        super().setUp()
        self.migration = MigrationRun.objects.create(state=MigrationStateChoices.CUTOVER)
        # The map as well as the step, because that is what a real fence records and replays.
        self.migration.journal['mapping'] = mapping.build_map()
        self.migration.record_step(cutover.STEP, counts={})

    def stage_and_validate(self):
        """Stage and validate, then pin that this suite starts from nothing activated."""
        super().stage_and_validate()
        self.assertFalse(ScriptProject.objects.filter(active_revision__isnull=False).exists())

    def test_it_refuses_before_the_fence_has_been_recorded(self):
        # Only one run may be open, so the fenced one goes before the fresh one is created.
        MigrationRun.objects.filter(pk=self.migration.pk).delete()
        fresh = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

        with self.assertRaises(cutover.CutoverRefused):
            cutover.activate_staged(fresh)

    def test_it_refuses_when_no_migration_is_open(self):
        with self.assertRaises(cutover.CutoverRefused):
            cutover.activate_staged(None)

    def test_activation_makes_the_custom_scripts_appear(self):
        self.stage_and_validate()
        self.assertFalse(CustomScript.objects.exists())

        results = cutover.activate_staged(self.migration)

        self.assertEqual(sorted(result['outcome'] for result in results), ['activated', 'activated'])
        self.assertEqual(ScriptProject.objects.filter(active_revision__isnull=False).count(), 2)
        # The class each legacy module published, now published by the plugin instead.
        self.assertEqual(sorted(CustomScript.objects.values_list('class_name', flat=True)), ['Deploy', 'Provision'])

    def test_the_step_is_recorded_with_every_outcome(self):
        self.stage_and_validate()

        cutover.activate_staged(self.migration)

        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(cutover.ACTIVATE_STEP))
        recorded = self.migration.journal['steps'][cutover.ACTIVATE_STEP]['projects']
        self.assertEqual(len(recorded), 2)

    def test_a_project_with_nothing_valid_is_recorded_and_skipped(self):
        self.stage_and_validate()
        # An operator fixes content and stages again, so this is reported rather than raised.
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        ScriptProjectRevision.objects.filter(project=project).update(status=RevisionStatusChoices.INVALID)

        results = cutover.activate_staged(self.migration)

        outcome = next(item for item in results if item['project_key'] == project.key)
        self.assertIn('no valid revision', outcome['outcome'])
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)
        # Its sibling still activated, so one project's content problem does not stop the pass.
        self.assertEqual(len([item for item in results if item['outcome'] == 'activated']), 1)

    def test_a_project_holding_no_revision_is_recorded_and_skipped(self):
        self.stage_and_validate()
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        ScriptProjectRevision.objects.filter(project=project).delete()

        results = cutover.activate_staged(self.migration)

        outcome = next(item for item in results if item['project_key'] == project.key)
        self.assertEqual(outcome['outcome'], 'holds no revision')
        self.assertIsNone(outcome['revision_pk'])

    def test_a_project_an_operator_made_by_hand_is_left_alone(self):
        self.stage_and_validate()
        untouched = ScriptProject.objects.create(
            name='hand made', key='hand-made', source_type=ProjectSourceTypeChoices.UPLOAD
        )

        results = cutover.activate_staged(self.migration)

        self.assertNotIn(untouched.key, [result['project_key'] for result in results])
        untouched.refresh_from_db()
        self.assertIsNone(untouched.active_revision_id)

    def test_a_second_run_repairs_rather_than_repeats(self):
        self.stage_and_validate()
        cutover.activate_staged(self.migration)
        serving = dict(ScriptProject.objects.values_list('key', 'active_revision_id'))
        script_pks = set(CustomScript.objects.values_list('pk', flat=True))

        results = cutover.activate_staged(self.migration)

        self.assertEqual(sorted(result['outcome'] for result in results), ['was already serving this revision'] * 2)
        self.assertEqual(dict(ScriptProject.objects.values_list('key', 'active_revision_id')), serving)
        # Synchronization skips a row that already matches, so the same rows survive untouched.
        self.assertEqual(set(CustomScript.objects.values_list('pk', flat=True)), script_pks)

    def test_a_second_run_over_fewer_modules_keeps_the_whole_record(self):
        # record_step assigns, so replacing the list would shrink what verification then checks.
        self.stage_and_validate()
        cutover.activate_staged(self.migration)
        self.migration.refresh_from_db()
        first = {entry['project_key'] for entry in self.migration.journal['steps'][cutover.ACTIVATE_STEP]['projects']}
        self.assertEqual(len(first), 2)

        ScriptModule.objects.filter(pk=self.uploaded.pk).delete()
        cutover.activate_staged(self.migration)

        self.migration.refresh_from_db()
        second = {entry['project_key'] for entry in self.migration.journal['steps'][cutover.ACTIVATE_STEP]['projects']}
        self.assertEqual(second, first)

    def test_the_job_reports_what_it_activated(self):
        self.stage_and_validate()

        job = MigrationActivationJob.enqueue(immediate=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(len(job.data['projects']), 2)
        messages = ' '.join(entry['message'] for entry in job.log_entries)
        self.assertIn('2 of 2 Script Project(s) are serving', messages)
        self.assertIn('publishing 2 Custom Script(s)', messages)

    def test_the_job_fails_when_the_fence_has_not_been_crossed(self):
        MigrationRun.objects.filter(pk=self.migration.pk).update(state=MigrationStateChoices.STAGING, journal={})

        job = MigrationActivationJob.enqueue(immediate=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertIn('cutover has not been entered', ' '.join(e['message'] for e in job.log_entries))
