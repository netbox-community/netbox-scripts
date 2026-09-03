from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse

from netbox_custom_scripts.choices import MigrationStateChoices
from netbox_custom_scripts.models import MigrationRun
from netbox_custom_scripts.models.migration import MIGRATION_LOCK_KEY, MIGRATION_LOCK_NAMESPACE, migration_lock

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

    def test_a_second_open_run_is_refused_by_the_database(self):
        # create() skips clean(), so the constraint is the only thing that can refuse this.
        MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

        with self.assertRaises(IntegrityError), transaction.atomic():
            MigrationRun.objects.create()

    def test_two_closed_runs_may_coexist(self):
        MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED)
        MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED)

        self.assertEqual(MigrationRun.objects.count(), 2)

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

    def test_completing_a_step_records_its_counts_and_its_warnings(self):
        run = MigrationRun.objects.create()

        run.complete_step('cleanup', {'modules': 2}, ['one module was left in place'])

        run.refresh_from_db()
        self.assertEqual(run.recorded_counts('cleanup'), {'modules': 2})
        self.assertEqual(run.warnings, ['one module was left in place'])

    def test_warnings_are_recorded_without_completing_a_step(self):
        run = MigrationRun.objects.create()

        run.record_warnings(['one module is blocked'])

        run.refresh_from_db()
        self.assertEqual(run.warnings, ['one module is blocked'])
        self.assertFalse(run.step_done('cleanup'))

    def test_a_restated_warning_is_recorded_once(self):
        # The second call repeats the first message and adds one.
        run = MigrationRun.objects.create()

        run.record_warnings(['one module is blocked'])
        run.record_warnings(['one module is blocked', 'and another'])

        run.refresh_from_db()
        self.assertEqual(run.warnings, ['one module is blocked', 'and another'])

    def test_a_step_recorded_from_a_stale_object_keeps_the_other(self):
        # Two passes can hold the same row as two Python objects, and the journal is one column,
        # so the second save used to write a dict that never contained the first step.
        run = MigrationRun.objects.create()
        stale = MigrationRun.objects.get(pk=run.pk)

        run.record_step('cutover')
        stale.record_step('activate')

        run.refresh_from_db()
        self.assertEqual(sorted(run.journal['steps']), ['activate', 'cutover'])

    def test_warnings_recorded_from_a_stale_object_keep_the_others(self):
        run = MigrationRun.objects.create()
        stale = MigrationRun.objects.get(pk=run.pk)

        run.record_warnings(['from the first pass'])
        stale.record_warnings(['from the second pass'])

        run.refresh_from_db()
        self.assertEqual(sorted(run.warnings), ['from the first pass', 'from the second pass'])

    def test_recording_a_second_step_keeps_the_first(self):
        run = MigrationRun.objects.create()

        run.record_step('cutover')
        run.record_step('activate')

        run.refresh_from_db()
        self.assertEqual(sorted(run.journal['steps']), ['activate', 'cutover'])


class MigrationLockTestCase(TransactionTestCase):
    """
    The run lock, proved in two halves against a genuinely separate session.

    TransactionTestCase, because a session-level advisory lock is only meaningful against
    another connection, and a TestCase would hide every write inside one that never commits.
    Racing threads are deliberately not used: they cannot be made deterministic.
    """

    @staticmethod
    def other_session_can_lock():
        """Report whether a separate session could take the run lock right now."""
        connection = connections.create_connection(DEFAULT_DB_ALIAS)
        connection.ensure_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', (MIGRATION_LOCK_NAMESPACE, MIGRATION_LOCK_KEY))
                acquired = cursor.fetchone()[0]
                if acquired:
                    cursor.execute('SELECT pg_advisory_unlock(%s, %s)', (MIGRATION_LOCK_NAMESPACE, MIGRATION_LOCK_KEY))
            return acquired
        finally:
            connection.close()

    def test_the_lock_excludes_another_session_and_releases(self):
        self.assertTrue(self.other_session_can_lock())

        with migration_lock():
            self.assertFalse(self.other_session_can_lock())

        self.assertTrue(self.other_session_can_lock())

    def test_the_lock_is_released_when_the_block_raises(self):
        # It is a session lock, so a rollback does not carry it away and the release has to run.
        with self.assertRaises(RuntimeError), migration_lock():
            raise RuntimeError('boom')

        self.assertTrue(self.other_session_can_lock())

    def test_it_nests_without_deadlocking(self):
        # PostgreSQL counts advisory locks, so a nested acquisition needs its own release.
        with migration_lock():
            with migration_lock():
                self.assertFalse(self.other_session_can_lock())
            self.assertFalse(self.other_session_can_lock())

        self.assertTrue(self.other_session_can_lock())
