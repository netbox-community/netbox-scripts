from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from netbox_custom_scripts.choices import MigrationStateChoices
from netbox_custom_scripts.models import MigrationRun

FORWARD = (
    MigrationStateChoices.LEGACY,
    MigrationStateChoices.STAGING,
    MigrationStateChoices.CUTOVER,
    MigrationStateChoices.MIGRATED,
)


class MigrationRunTestCase(TestCase):
    """A migration run opens once, moves forward only, and journals what each step completed."""

    def test_a_new_run_starts_in_the_legacy_state(self):
        run = MigrationRun.objects.create()

        self.assertEqual(run.state, MigrationStateChoices.LEGACY)
        self.assertIsNone(run.cutover_started)
        self.assertIsNone(run.completed)
        self.assertEqual(run.journal, {})
        self.assertFalse(run.is_closed)

    def test_str_and_absolute_url(self):
        run = MigrationRun.objects.create()

        self.assertEqual(str(run), f'Custom Script migration {run.pk}')
        self.assertEqual(
            run.get_absolute_url(),
            reverse('plugins:netbox_custom_scripts:migrationrun', args=[run.pk]),
        )

    def test_start_records_the_versions_the_migration_began_against(self):
        run = MigrationRun.start()

        self.assertTrue(run.netbox_version)
        self.assertTrue(run.plugin_version)
        self.assertEqual(run.state, MigrationStateChoices.LEGACY)

    def test_advancing_walks_the_states_in_order_and_stamps_each_time(self):
        run = MigrationRun.objects.create()

        run.advance(MigrationStateChoices.STAGING)
        self.assertIsNone(run.cutover_started)

        run.advance(MigrationStateChoices.CUTOVER)
        self.assertIsNotNone(run.cutover_started)
        self.assertIsNone(run.completed)

        run.advance(MigrationStateChoices.MIGRATED)
        self.assertIsNotNone(run.completed)

        run.refresh_from_db()
        self.assertEqual(run.state, MigrationStateChoices.MIGRATED)
        self.assertTrue(run.is_closed)

    def test_advancing_refuses_every_move_that_is_not_the_next_one(self):
        # Skipping a state would mean a step ran without the one it depends on, and moving back
        # would mean the cutover could be undone, which it cannot.
        run = MigrationRun.objects.create()
        for position, state in enumerate(FORWARD):
            allowed = FORWARD[position + 1] if position + 1 < len(FORWARD) else None
            for target in FORWARD:
                if target == allowed:
                    continue
                with self.subTest(current=state, requested=target):
                    run.state = state
                    with self.assertRaises(ValidationError):
                        run.advance(target)

    def test_a_closed_run_cannot_advance_any_further(self):
        run = MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED)

        for target in FORWARD:
            with self.subTest(requested=target), self.assertRaises(ValidationError):
                run.advance(target)

    def test_only_one_run_may_be_open(self):
        MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

        with self.assertRaises(ValidationError):
            MigrationRun().full_clean()

    def test_a_new_run_may_open_once_the_previous_one_finished(self):
        MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED)

        second = MigrationRun.start()

        self.assertEqual(second.state, MigrationStateChoices.LEGACY)

    def test_validating_an_open_run_does_not_refuse_itself(self):
        run = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

        run.full_clean()

    def test_current_finds_the_open_run_and_ignores_a_finished_one(self):
        self.assertIsNone(MigrationRun.current())

        closed = MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED)
        self.assertIsNone(MigrationRun.current())

        run = MigrationRun.objects.create(state=MigrationStateChoices.CUTOVER)
        self.assertEqual(MigrationRun.current(), run)
        self.assertNotEqual(MigrationRun.current(), closed)

    def test_a_recorded_step_is_journalled_with_its_detail_and_reads_back(self):
        run = MigrationRun.objects.create()

        self.assertFalse(run.step_done('cutover'))

        run.record_step('cutover', permissions=3)

        run.refresh_from_db()
        self.assertTrue(run.step_done('cutover'))
        self.assertEqual(run.journal['steps']['cutover']['permissions'], 3)
        self.assertTrue(run.journal['steps']['cutover']['completed'])
        self.assertFalse(run.step_done('repoint'))

    def test_recording_a_second_step_keeps_the_first(self):
        run = MigrationRun.objects.create()

        run.record_step('cutover')
        run.record_step('activate')

        run.refresh_from_db()
        self.assertEqual(sorted(run.journal['steps']), ['activate', 'cutover'])
