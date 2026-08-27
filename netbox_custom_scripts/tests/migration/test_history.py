import uuid
from datetime import timedelta
from unittest.mock import patch

import django_rq
from django.test import TestCase
from django.utils import timezone
from rq.job import Job as RQJob

from core.choices import JobStatusChoices
from core.models import Job, ObjectType
from dcim.models import Site
from extras.models import Script, ScriptModule
from netbox_custom_scripts.migration import cutover, references
from netbox_custom_scripts.models import CustomScript
from netbox_custom_scripts.runtime.exceptions import EntrypointImportError
from netbox_custom_scripts.tests.migration.test_references import ReferenceMigrationMixin

# A legacy script with a variable, so the journal's primary keys have something to resolve back to.
VARIABLE_SCRIPT = b"""from extras.scripts import ObjectVar, Script
from dcim.models import Site


class Retire(Script):
    site = ObjectVar(model=Site)

    def run(self, data, commit):
        return data['site'].name
"""


class LegacyJobMixin(ReferenceMigrationMixin):
    """The built-in content plus a Job history and a queue to read schedules out of."""

    def setUp(self):
        super().setUp()
        self.variable_module = self.legacy_synced_module('automation/retire.py', VARIABLE_SCRIPT)
        self.variable_script = Script.objects.get(module=self.variable_module, name='Retire')
        self.site = Site.objects.create(name='HQ', slug='hq')
        self.queue = django_rq.get_queue('default')
        self.queue.empty()
        self.addCleanup(self.queue.empty)

    def legacy_job(self, *, script=None, status=JobStatusChoices.STATUS_COMPLETED, **fields):
        """One built-in Script Job row, in whatever state the case needs."""
        return Job.objects.create(
            name='Deploy',
            object_type=self.script_type,
            object_id=(script or self.script).pk,
            job_id=uuid.uuid4(),
            status=status,
            queue_name='default',
            **fields,
        )

    def legacy_schedule(self, *, script=None, data=None, commit=True, scheduled=None, interval=None, timeout=600):
        """One waiting built-in Job with a fetchable task, which is where its input lives."""
        job = self.legacy_job(
            script=script,
            status=JobStatusChoices.STATUS_SCHEDULED if scheduled else JobStatusChoices.STATUS_PENDING,
            scheduled=scheduled,
            interval=interval,
        )
        task = RQJob.create(
            func='netbox.jobs.JobRunner.handle',
            kwargs={'data': data or {}, 'commit': commit},
            connection=self.queue.connection,
            id=str(job.job_id),
            timeout=timeout,
        )
        task.save()
        self.addCleanup(self.queue.connection.delete, f'rq:job:{job.job_id}')
        return job

    def plugin_script(self, class_name='Deploy'):
        return CustomScript.objects.get(class_name=class_name)


class RepointJobHistoryTestCase(LegacyJobMixin, TestCase):
    """Job history moves onto the Custom Script before anything can delete it."""

    def test_a_completed_job_is_reachable_from_the_custom_script(self):
        job = self.legacy_job()
        self.cross_over()

        counts, warnings = references.repoint_job_history(self.migration)

        script = self.plugin_script()
        job.refresh_from_db()
        self.assertEqual(job.object_id, script.pk)
        self.assertEqual(job.object_type_id, ObjectType.objects.get_for_model(CustomScript).pk)
        # The Jobs tab reads this relation, so being reachable through it is the whole point.
        self.assertIn(job.pk, [item.pk for item in script.jobs.all()])
        self.assertEqual(counts['moved'], 1)
        self.assertEqual(warnings, [])

    def test_a_failed_job_moves_with_the_rest(self):
        # History is history: a run that failed is what an operator most wants to still find.
        job = self.legacy_job(status=JobStatusChoices.STATUS_FAILED)
        self.cross_over()

        references.repoint_job_history(self.migration)

        job.refresh_from_db()
        self.assertEqual(job.object_id, self.plugin_script().pk)

    def test_every_job_of_one_script_moves_in_one_statement(self):
        jobs = [self.legacy_job() for _ in range(3)]
        self.cross_over()

        counts, _warnings = references.repoint_job_history(self.migration)

        self.assertEqual(counts['moved'], 3)
        script = self.plugin_script()
        for job in jobs:
            job.refresh_from_db()
            self.assertEqual(job.object_id, script.pk)

    def test_a_job_whose_script_does_not_resolve_is_left_where_it_is(self):
        job = self.legacy_job()
        self.cross_over()
        # An orphaned history row beats a wrong one, so this is reported rather than guessed at.
        self.plugin_script().delete()

        counts, warnings = references.repoint_job_history(self.migration)

        job.refresh_from_db()
        self.assertEqual(job.object_id, self.script.pk)
        self.assertEqual(job.object_type_id, self.script_type.pk)
        self.assertEqual(counts['unresolved'], 1)
        self.assertTrue(any(str(self.script.pk) in warning for warning in warnings))
        # Left open, or repairing the Project would meet a pass that returns and does nothing.
        self.migration.refresh_from_db()
        self.assertFalse(self.migration.step_done(references.HISTORY_STEP))

    def test_a_repaired_script_gets_its_history_moved_on_the_next_pass(self):
        job = self.legacy_job()
        self.cross_over()
        self.plugin_script().delete()
        references.repoint_job_history(self.migration)
        self.migration.refresh_from_db()

        # Activation is re-runnable, and its promotion callback recreates the derived rows.
        cutover.activate_staged(self.migration)
        self.migration.refresh_from_db()
        counts, _warnings = references.repoint_job_history(self.migration)

        self.assertEqual(counts['unresolved'], 0)
        job.refresh_from_db()
        self.assertEqual(job.object_id, self.plugin_script().pk)
        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(references.HISTORY_STEP))

    def test_a_job_naming_a_built_in_module_is_reported_rather_than_repointed(self):
        # A Custom Script Project holds no jobs, and this key could collide with a Script's, which
        # is why the queries scope on the object type rather than on the key alone.
        module_type = ObjectType.objects.get_for_model(ScriptModule, for_concrete_model=False)
        job = Job.objects.create(
            name='sync', object_type=module_type, object_id=self.synced.pk, job_id=uuid.uuid4(), queue_name='default'
        )
        self.cross_over()

        counts, warnings = references.repoint_job_history(self.migration)

        job.refresh_from_db()
        self.assertEqual(job.object_type_id, module_type.pk)
        self.assertEqual(job.object_id, self.synced.pk)
        self.assertEqual(counts['modules'], 1)
        self.assertTrue(any('module' in warning for warning in warnings))

    def test_a_second_pass_returns_the_recorded_counts(self):
        self.legacy_job()
        self.cross_over()
        first, _warnings = references.repoint_job_history(self.migration)
        self.migration.refresh_from_db()

        second, warnings = references.repoint_job_history(self.migration)

        self.assertEqual(second, first)
        self.assertEqual(warnings, [])


class RecreateSchedulesTestCase(LegacyJobMixin, TestCase):
    """Every schedule the fence cancelled goes back into service against the Custom Script."""

    def future(self):
        return timezone.now() + timedelta(days=1)

    def past(self):
        return timezone.now() - timedelta(hours=1)

    def new_jobs(self):
        """Return the plugin's own Jobs, which are the ones a recreation produced."""
        return Job.objects.filter(object_type=ObjectType.objects.get_for_model(CustomScript))

    def test_a_pending_schedule_comes_back_with_its_input_resolved(self):
        # The captured value is a key, and a form is what turns it back into the instance an
        # ObjectVar hands the script.
        self.legacy_schedule(script=self.variable_script, data={'site': self.site}, commit=False)
        self.cross_over()

        with self.captureOnCommitCallbacks(execute=True):
            counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 1)
        # Warned rather than silent: a queued run has no time to keep, so it is replayed at once.
        self.assertTrue(any('run at once' in warning for warning in warnings))
        self.assertEqual(self.queue.count, 1)
        task = self.queue.jobs[0]
        self.assertEqual(task.kwargs['data'], {'site': self.site})
        self.assertFalse(task.kwargs['commit'])
        self.assertEqual(task.timeout, 600)

    def test_a_future_schedule_keeps_its_time_and_pins_the_active_revision(self):
        due = self.future()
        self.legacy_schedule(scheduled=due)
        self.cross_over()

        counts, _warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 1)
        job = self.new_jobs().get()
        self.assertEqual(job.scheduled, due)
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)
        self.assertEqual(job.object_id, self.plugin_script().pk)
        self.assertIsNotNone(job.data['revision_id'])

    def test_a_schedule_naming_a_module_is_skipped_rather_than_resolved_by_key(self):
        # Both built-in types are captured and their key sequences are independent.
        module_type = ObjectType.objects.get_for_model(ScriptModule, for_concrete_model=False)
        job = self.legacy_schedule(scheduled=self.future())
        Job.objects.filter(pk=job.pk).update(object_type=module_type, object_id=self.plugin_script_pk())
        self.cross_over()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 0)
        self.assertEqual(counts['skipped'], 1)
        self.assertFalse(self.new_jobs().exists())
        self.assertTrue(any('script module' in warning for warning in warnings))

    def plugin_script_pk(self):
        """A built-in Script key to collide with, which is what makes the bare lookup unsafe."""
        return self.script.pk

    def test_a_queued_run_is_recreated_to_run_at_once_and_says_so(self):
        # The one case where this pass executes an operator's script, so it has to be reported.
        self.legacy_schedule()
        self.cross_over()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 1)
        job = self.new_jobs().get()
        self.assertIsNone(job.scheduled)
        self.assertIsNone(job.interval)
        self.assertTrue(any('run at once' in warning for warning in warnings))

    def test_a_recurrence_keeps_its_interval_and_pins_nothing(self):
        # A recurrence resolves the active revision per occurrence, so a pin would freeze it.
        self.legacy_schedule(scheduled=self.future(), interval=60)
        self.cross_over()

        counts, _warnings = references.recreate_schedules(self.migration)

        job = self.new_jobs().get()
        self.assertEqual(job.interval, 60)
        self.assertIsNone(job.data['revision_id'])
        self.assertEqual(counts['shifted'], 0)

    def test_a_one_shot_whose_time_has_passed_is_refused_rather_than_run(self):
        # rq runs a past-due job the moment it is enqueued, and a migration must not run an
        # operator's script unasked.
        self.legacy_schedule(scheduled=self.past())
        self.cross_over()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts, {'recreated': 0, 'skipped': 1, 'shifted': 0, 'outstanding': 0})
        self.assertFalse(self.new_jobs().exists())
        self.assertTrue(any('has passed' in warning for warning in warnings))
        # Nothing a re-run would clear, so this must not hold the step open forever.
        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_recurrence_whose_time_has_passed_starts_now_and_keeps_its_interval(self):
        self.legacy_schedule(scheduled=self.past(), interval=30)
        self.cross_over()

        counts, warnings = references.recreate_schedules(self.migration)

        job = self.new_jobs().get()
        self.assertIsNone(job.scheduled)
        self.assertEqual(job.interval, 30)
        self.assertEqual(counts, {'recreated': 1, 'skipped': 0, 'shifted': 1, 'outstanding': 0})
        self.assertTrue(any('30 minute interval' in warning for warning in warnings))

    def test_input_that_no_longer_validates_is_reported_and_skipped(self):
        self.legacy_schedule(script=self.variable_script, data={'site': self.site})
        self.cross_over()
        # An operator deleted the object the schedule named, which is their decision, not ours.
        self.site.delete()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['outstanding'], 0)
        self.assertFalse(self.new_jobs().exists())
        self.assertTrue(any('site' in warning for warning in warnings))
        # The operator removed what it named, so re-running would report the same thing forever.
        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_retired_script_is_reported_rather_than_scheduled(self):
        self.legacy_schedule()
        self.cross_over()
        CustomScript.objects.filter(pk=self.plugin_script().pk).update(is_retired=True)

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['skipped'], 1)
        self.assertFalse(self.new_jobs().exists())
        self.assertTrue(any('retired' in warning for warning in warnings))

    def test_a_schedule_whose_script_does_not_resolve_is_reported(self):
        self.legacy_schedule()
        self.cross_over()
        self.plugin_script().delete()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['outstanding'], 1)
        self.assertTrue(any(str(self.script.pk) in warning for warning in warnings))
        self.migration.refresh_from_db()
        self.assertFalse(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_load_failure_outside_the_narrow_set_is_warned_rather_than_killing_the_pass(self):
        # Not rooted in StorageError, so it escaped the replay clause and killed the job mid-step.
        self.legacy_schedule(scheduled=self.future())
        self.cross_over()

        with patch(
            'netbox_custom_scripts.migration.references.load_script_class',
            side_effect=EntrypointImportError('deploy imports a module that is not there', {}),
        ):
            counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['outstanding'], 1)
        self.assertTrue(any('not there' in warning for warning in warnings))
        self.migration.refresh_from_db()
        self.assertFalse(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_repaired_script_gets_its_schedule_recreated_on_the_next_pass(self):
        self.legacy_schedule(scheduled=self.future())
        self.cross_over()
        self.plugin_script().delete()
        references.recreate_schedules(self.migration)
        self.migration.refresh_from_db()

        cutover.activate_staged(self.migration)
        self.migration.refresh_from_db()
        counts, _warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 1)
        self.assertEqual(counts['outstanding'], 0)
        self.assertEqual(self.new_jobs().get().object_id, self.plugin_script().pk)
        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_schedule_naming_a_departed_class_does_not_hold_the_step_open(self):
        # The frozen map excludes a soft-deleted class, so this entry can never resolve and
        # holding the step open for it would leave a migration that can never close.
        departed = Script.objects.create(module=self.synced, name='Gone')
        self.legacy_schedule(script=departed, scheduled=self.future())
        Script.objects.filter(pk=departed.pk).update(is_executable=False)
        self.cross_over()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['skipped'], 1)
        self.assertEqual(counts['outstanding'], 0)
        self.assertTrue(any('left its file' in warning for warning in warnings))
        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(references.SCHEDULES_STEP))

    def test_a_schedule_belonging_to_a_deleted_user_is_recreated_with_no_owner(self):
        from django.contrib.auth import get_user_model

        user = get_user_model().objects.create_user(username='departed')
        job = self.legacy_schedule()
        Job.objects.filter(pk=job.pk).update(user=user)
        self.cross_over()
        user.delete()

        counts, warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 1)
        self.assertIsNone(self.new_jobs().get().user)
        self.assertTrue(any('no longer exists' in warning for warning in warnings))

    def test_a_second_pass_creates_no_duplicate(self):
        # The one step in the phase that is not naturally idempotent, so the journal is the guard.
        legacy = self.legacy_schedule()
        self.cross_over()
        references.recreate_schedules(self.migration)
        self.migration.refresh_from_db()
        created = self.new_jobs().get()
        # What a crash between the enqueue and the step record leaves behind.
        self.migration.journal['steps'].pop(references.SCHEDULES_STEP)
        self.migration.save(update_fields=('journal',))

        counts, _warnings = references.recreate_schedules(self.migration)

        self.assertEqual(counts['recreated'], 0)
        self.assertEqual([job.pk for job in self.new_jobs()], [created.pk])
        self.migration.refresh_from_db()
        self.assertEqual(self.migration.journal['recreated_schedules'], {str(legacy.pk): created.pk})
