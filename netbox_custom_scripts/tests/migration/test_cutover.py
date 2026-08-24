import hashlib
import shutil
import tempfile
import uuid

import django_rq
from django.test import TestCase, override_settings
from django.utils import timezone
from rq.job import Job as RQJob

from core.choices import JobStatusChoices, ManagedFileRootPathChoices
from core.events import OBJECT_UPDATED
from core.models import AutoSyncRecord, DataFile, DataSource, Job, ObjectType
from dcim.models import Site
from extras.models import EventRule, Script, ScriptModule, Webhook
from netbox_custom_scripts import activation
from netbox_custom_scripts.choices import MigrationStateChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.migration import cutover, mapping, plan, staging
from netbox_custom_scripts.migration import source as legacy_source
from netbox_custom_scripts.models import CustomScriptProjectRevision, MigrationRun
from netbox_custom_scripts.tests.migration.test_staging import LEGACY_SCRIPT as SYNCED_SCRIPT
from netbox_custom_scripts.tests.migration.test_staging import LegacySourceMixin
from users.models import ObjectPermission

LEGACY_SCRIPT = b"""from extras.scripts import Script


class Deploy(Script):
    def run(self, data, commit):
        pass
"""


class CutoverTestCase(TestCase):
    """Crossing the fence: what it refuses, what it records, and what it closes."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        scripts_root = tempfile.mkdtemp(prefix='legacy-scripts-')
        cls.addClassCleanup(shutil.rmtree, scripts_root, ignore_errors=True)
        cls.enterClassContext(
            override_settings(
                STORAGES={
                    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
                    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
                    'scripts': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': scripts_root, 'allow_overwrite': True},
                    },
                    'netbox_custom_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
                }
            )
        )

    def setUp(self):
        self.migration = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)
        self.source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/a')
        self.data_file = DataFile.objects.create(
            source=self.source,
            path='automation/deploy.py',
            size=len(LEGACY_SCRIPT),
            hash=hashlib.sha256(LEGACY_SCRIPT).hexdigest(),
            data=LEGACY_SCRIPT,
            last_updated=timezone.now(),
        )
        self.module = ScriptModule(
            file_root=ManagedFileRootPathChoices.SCRIPTS,
            data_file=self.data_file,
            auto_sync_enabled=True,
        )
        self.module.full_clean()
        self.module.save()
        # Saving a module runs the built-in feature's own discovery, which creates the Script row,
        # so this adopts that row rather than competing with it for the unique name.
        self.script, _created = Script.objects.get_or_create(module=self.module, name='Deploy')
        self.script_type = ObjectType.objects.get_for_model(Script, for_concrete_model=False)
        # The fence refuses unless what was staged can be served. The verdict is forced rather
        # than validated, because this suite covers what the crossing captures and closes.
        modules = legacy_source.legacy_modules()
        staged = staging.stage(plan.group(modules), modules)
        CustomScriptProjectRevision.objects.filter(pk__in=[item['revision_pk'] for item in staged]).update(
            status=RevisionStatusChoices.VALID
        )

    def legacy_permission(self, *, actions=('view', 'run'), constraints=None, extra_type=None):
        permission = ObjectPermission.objects.create(
            name='built-in scripts',
            actions=list(actions),
            constraints=constraints,
        )
        permission.object_types.add(self.script_type)
        if extra_type is not None:
            permission.object_types.add(extra_type)
        return permission

    def legacy_action_rule(self):
        rule = EventRule.objects.create(
            name='on device change',
            event_types=[OBJECT_UPDATED],
            action_type='script',
            action_object_type=self.script_type,
            action_object_id=self.script.pk,
        )
        rule.object_types.add(ObjectType.objects.get_for_model(Site))
        return rule

    def legacy_source_rule(self):
        # A rule watching the built-in Scripts themselves, firing a webhook. Its action is not
        # legacy, so only its source content type has to move.
        webhook = Webhook.objects.create(name='notify', payload_url='http://localhost/hook')
        rule = EventRule.objects.create(
            name='on script change',
            event_types=[OBJECT_UPDATED],
            action_type='webhook',
            action_object_type=ObjectType.objects.get_for_model(Webhook),
            action_object_id=webhook.pk,
        )
        rule.object_types.add(self.script_type)
        return rule

    def legacy_job(self, *, status=JobStatusChoices.STATUS_SCHEDULED, task_kwargs=None, interval=None):
        job = Job.objects.create(
            name='Deploy',
            object_type=self.script_type,
            object_id=self.script.pk,
            job_id=uuid.uuid4(),
            status=status,
            queue_name='default',
            scheduled=timezone.now() if status == JobStatusChoices.STATUS_SCHEDULED else None,
            interval=interval,
        )
        if task_kwargs is not None:
            self.rq_task(job, task_kwargs)
        return job

    def rq_task(self, job, kwargs):
        """Put a fetchable RQ task in place for one Job row, with no worker involved."""
        queue = django_rq.get_queue(job.queue_name)
        task = RQJob.create(
            func='netbox.jobs.JobRunner.handle',
            kwargs=kwargs,
            connection=queue.connection,
            id=str(job.job_id),
            timeout=600,
        )
        task.save()
        self.addCleanup(self.discard_task, queue, str(job.job_id))
        return task

    @staticmethod
    def discard_task(queue, task_id):
        queue.connection.delete(f'rq:job:{task_id}')

    def test_it_refuses_when_nothing_has_been_staged(self):
        with self.assertRaises(cutover.CutoverRefused):
            cutover.enter_cutover(None)

    def test_it_refuses_from_a_state_that_cannot_cross(self):
        self.migration.state = MigrationStateChoices.LEGACY
        self.migration.save(update_fields=('state',))

        with self.assertRaises(cutover.CutoverRefused):
            cutover.enter_cutover(self.migration)

    def test_it_refuses_while_a_built_in_script_job_is_running(self):
        # A hard block rather than a warning: a run mid-flight must not be cut out from under.
        running = self.legacy_job(status=JobStatusChoices.STATUS_RUNNING)

        with self.assertRaises(cutover.CutoverRefused) as caught:
            cutover.enter_cutover(self.migration)

        self.assertIn(str(running.pk), str(caught.exception))
        self.migration.refresh_from_db()
        self.assertEqual(self.migration.state, MigrationStateChoices.STAGING)

    def test_a_permission_is_captured_with_who_holds_it_and_what_else_it_names(self):
        site_type = ObjectType.objects.get_for_model(Site)
        permission = self.legacy_permission(actions=('view', 'run'), extra_type=site_type)

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        captured = self.migration.journal['permissions']
        self.assertEqual([entry['pk'] for entry in captured], [permission.pk])
        self.assertEqual(captured[0]['actions'], ['view', 'run'])
        self.assertTrue(captured[0]['enabled'])
        self.assertEqual(captured[0]['legacy_object_types'], ['extras.script'])
        self.assertEqual(captured[0]['other_object_types'], ['dcim.site'])

    def test_an_action_rule_and_a_source_rule_are_captured_separately(self):
        action_rule = self.legacy_action_rule()
        source_rule = self.legacy_source_rule()

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        captured = {entry['pk']: entry for entry in self.migration.journal['event_rules']}
        self.assertEqual(captured[action_rule.pk]['action_object_id'], self.script.pk)
        self.assertEqual(captured[action_rule.pk]['legacy_source_types'], [])
        self.assertIsNone(captured[source_rule.pk]['action_object_id'])
        self.assertEqual(captured[source_rule.pk]['legacy_source_types'], ['extras.script'])

    def test_a_rule_needing_both_changes_records_both(self):
        rule = self.legacy_action_rule()
        rule.object_types.add(self.script_type)

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        entry = next(item for item in self.migration.journal['event_rules'] if item['pk'] == rule.pk)
        self.assertEqual(entry['action_object_id'], self.script.pk)
        self.assertEqual(entry['legacy_source_types'], ['extras.script'])

    def test_a_schedule_is_captured_with_the_input_read_out_of_the_queue(self):
        # Input lives only in the RQ task, so the row alone cannot say what the run would do.
        job = self.legacy_job(interval=60, task_kwargs={'data': {'name': 'core'}, 'commit': False})

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        captured = self.migration.journal['schedules']
        self.assertEqual([entry['job_pk'] for entry in captured], [job.pk])
        self.assertEqual(captured[0]['data'], {'name': 'core'})
        self.assertFalse(captured[0]['commit'])
        self.assertEqual(captured[0]['interval'], 60)
        self.assertEqual(captured[0]['legacy_script_pk'], self.script.pk)
        self.assertEqual(captured[0]['job_timeout'], 600)

    def test_a_model_instance_in_the_input_is_captured_as_its_key(self):
        site = Site.objects.create(name='HQ', slug='hq')
        self.legacy_job(task_kwargs={'data': {'site': site, 'others': [site]}, 'commit': True})

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        data = self.migration.journal['schedules'][0]['data']
        self.assertEqual(data['site'], site.pk)
        self.assertEqual(data['others'], [site.pk])

    def test_input_that_cannot_be_recorded_is_reported_and_left_out(self):
        # An uploaded file is gone once the request ended, so it can never be replayed.
        self.legacy_job(task_kwargs={'data': {'name': 'core', 'upload': object()}, 'commit': True})

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.journal['schedules'][0]['data'], {'name': 'core'})
        self.assertTrue(any('upload' in warning for warning in self.migration.warnings))

    def test_a_schedule_whose_task_the_queue_lost_is_reported_rather_than_raised(self):
        job = self.legacy_job(task_kwargs=None)

        counts = cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.journal['schedules'], [])
        self.assertEqual(counts['schedules'], 0)
        self.assertTrue(any(str(job.pk) in warning for warning in self.migration.warnings))

    def test_a_terminal_job_is_not_captured(self):
        self.legacy_job(status=JobStatusChoices.STATUS_COMPLETED)

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.journal['schedules'], [])

    def test_the_permission_is_withdrawn(self):
        permission = self.legacy_permission()

        counts = cutover.enter_cutover(self.migration)

        permission.refresh_from_db()
        self.assertFalse(permission.enabled)
        self.assertEqual(counts['permissions'], 1)

    def test_every_captured_rule_stops_firing(self):
        action_rule = self.legacy_action_rule()
        source_rule = self.legacy_source_rule()

        counts = cutover.enter_cutover(self.migration)

        action_rule.refresh_from_db()
        source_rule.refresh_from_db()
        self.assertFalse(action_rule.enabled)
        self.assertFalse(source_rule.enabled)
        self.assertEqual(counts['event_rules'], 2)

    def test_a_stale_built_in_job_cannot_execute_after_the_fence(self):
        # Security-relevant rather than tidy: the task is gone from the queue and the row is no
        # longer in an enqueued state, so nothing can pick it up.
        job = self.legacy_job(task_kwargs={'data': {}, 'commit': True})
        queue = django_rq.get_queue(job.queue_name)

        counts = cutover.enter_cutover(self.migration)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertNotIn(job.status, JobStatusChoices.ENQUEUED_STATE_CHOICES)
        self.assertFalse(RQJob.exists(str(job.job_id), connection=queue.connection))
        self.assertIn('migration', job.error)
        self.assertEqual(counts['schedules'], 1)

    def test_a_job_that_started_during_the_capture_is_not_cancelled_underneath_itself(self):
        # The capture excludes a running job on purpose. With a task, or it is skipped and proves nothing.
        job = self.legacy_job(task_kwargs={})
        cutover._capture(self.migration)
        self.assertEqual(len(self.migration.journal['schedules']), 1)
        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING)

        counts = cutover._close(self.migration.journal)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_RUNNING)
        self.assertEqual(counts['schedules'], 0)

    def test_a_resumed_close_reports_what_is_closed_not_what_it_changed(self):
        # Counting the delta made a resumed pass report zeroes for what the first attempt closed.
        permission = self.legacy_permission()
        rule = self.legacy_action_rule()
        cutover._capture(self.migration)
        first = cutover._close(self.migration.journal)

        second = cutover._close(self.migration.journal)

        self.assertEqual(first['permissions'], second['permissions'])
        self.assertEqual(first['event_rules'], second['event_rules'])
        self.assertGreaterEqual(second['permissions'], 1)
        permission.refresh_from_db()
        rule.refresh_from_db()
        self.assertFalse(permission.enabled)
        self.assertFalse(rule.enabled)

    def test_the_deregistered_records_are_journalled(self):
        # The only closure with no record of what it removed, so a manual restore had nothing to read.
        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.journal['auto_sync'], [self.module.pk])

    def test_a_report_keeps_its_synchronization(self):
        # A report is not this migration's, so dropping its record would silently stop it updating.
        data_file = DataFile.objects.create(
            source=self.source,
            path='audit.py',
            size=len(LEGACY_SCRIPT),
            hash=hashlib.sha256(LEGACY_SCRIPT).hexdigest(),
            data=LEGACY_SCRIPT,
            last_updated=timezone.now(),
        )
        report = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=data_file, auto_sync_enabled=True)
        report.full_clean()
        report.save()
        ScriptModule.objects.filter(pk=report.pk).update(file_root=ManagedFileRootPathChoices.REPORTS)
        concrete = ObjectType.objects.get_for_model(ScriptModule)

        cutover.enter_cutover(self.migration)

        self.assertTrue(AutoSyncRecord.objects.filter(object_type=concrete, object_id=report.pk).exists())

    def test_the_source_directory_is_deregistered_from_synchronization(self):
        concrete = ObjectType.objects.get_for_model(ScriptModule)
        self.assertTrue(AutoSyncRecord.objects.filter(object_type=concrete, object_id=self.module.pk).exists())

        counts = cutover.enter_cutover(self.migration)

        self.assertFalse(AutoSyncRecord.objects.filter(object_type=concrete, object_id=self.module.pk).exists())
        self.assertEqual(counts['auto_sync'], 1)

    def test_nothing_that_carries_history_is_deleted(self):
        completed = self.legacy_job(status=JobStatusChoices.STATUS_COMPLETED)

        cutover.enter_cutover(self.migration)

        self.assertTrue(Script.objects.filter(pk=self.script.pk).exists())
        self.assertTrue(ScriptModule.objects.filter(pk=self.module.pk).exists())
        self.assertTrue(Job.objects.filter(pk=completed.pk).exists())

    def test_crossing_records_the_state_the_time_and_the_step(self):
        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.state, MigrationStateChoices.CUTOVER)
        self.assertIsNotNone(self.migration.cutover_started)
        self.assertTrue(self.migration.step_done(cutover.STEP))

    def test_a_second_run_changes_nothing_and_returns_the_recorded_counts(self):
        # Later steps re-enable what the fence disabled, so a repeat must not undo them.
        permission = self.legacy_permission()
        rule = self.legacy_action_rule()
        first = cutover.enter_cutover(self.migration)
        self.migration.refresh_from_db()
        ObjectPermission.objects.filter(pk=permission.pk).update(enabled=True)
        EventRule.objects.filter(pk=rule.pk).update(enabled=True)

        second = cutover.enter_cutover(self.migration)

        self.assertEqual(second, first)
        permission.refresh_from_db()
        rule.refresh_from_db()
        self.assertTrue(permission.enabled)
        self.assertTrue(rule.enabled)

    def test_an_interrupted_fence_finishes_the_closures_without_recapturing(self):
        permission = self.legacy_permission()
        # What a crash between the capture and the closures leaves behind.
        self.migration.journal['permissions'] = [
            {
                'pk': permission.pk,
                'name': permission.name,
                'description': '',
                'enabled': True,
                'actions': ['view'],
                'constraints': None,
                'users': [],
                'groups': [],
                'legacy_object_types': ['extras.script'],
                'other_object_types': [],
            }
        ]
        self.migration.journal['event_rules'] = []
        self.migration.journal['schedules'] = []
        self.migration.save(update_fields=('journal',))

        counts = cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        permission.refresh_from_db()
        self.assertFalse(permission.enabled)
        self.assertEqual(counts['permissions'], 1)
        # The recorded actions are the captured ones, not a re-read of the row it just closed.
        self.assertEqual(self.migration.journal['permissions'][0]['actions'], ['view'])

    def test_require_staged_refuses_until_the_fence_is_recorded(self):
        with self.assertRaises(cutover.CutoverRefused):
            cutover.require_staged(self.migration)

        cutover.enter_cutover(self.migration)
        self.migration.refresh_from_db()

        self.assertEqual(cutover.require_staged(self.migration), self.migration)


class CutoverServabilityTestCase(LegacySourceMixin, TestCase):
    """The fence refuses unless every Project this migration mapped can serve something past it."""

    def setUp(self):
        super().setUp()
        self.migration = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

    def refuse_project(self, status):
        """Put one staged Project's revisions into a status activation would not accept."""
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        CustomScriptProjectRevision.objects.filter(project=project).update(status=status)
        return project

    def crash_after_closing(self, state):
        """Leave the run as a crash between the closures and the step record would leave it."""
        self.migration.journal['steps'].pop(cutover.STEP)
        MigrationRun.objects.filter(pk=self.migration.pk).update(state=state, journal=self.migration.journal)
        self.migration.refresh_from_db()

    def test_a_project_whose_revision_was_refused_stops_the_fence(self):
        self.stage_and_validate()
        project = self.refuse_project(RevisionStatusChoices.INVALID)

        with self.assertRaises(cutover.CutoverRefused) as caught:
            cutover.enter_cutover(self.migration)

        self.assertIn(project.key, str(caught.exception))
        self.migration.refresh_from_db()
        self.assertEqual(self.migration.state, MigrationStateChoices.STAGING)
        self.assertFalse(self.migration.step_done(cutover.STEP))

    def test_a_retired_revision_does_not_count_as_servable(self):
        # ACTIVATABLE_REVISION_STATUSES admits retired and activation refuses it, so the guard
        # cannot be written against that tuple without passing a project it would then refuse.
        self.stage_and_validate()
        project = self.refuse_project(RevisionStatusChoices.RETIRED)

        with self.assertRaises(cutover.CutoverRefused) as caught:
            cutover.enter_cutover(self.migration)

        self.assertIn(project.key, str(caught.exception))

    def test_a_revision_still_awaiting_a_verdict_says_so(self):
        # The common case: the page is opened before the worker validates what staging created.
        self.stage_all()

        blocked = cutover.unservable_projects(self.migration)

        self.assertEqual(len(blocked), 2)
        for entry in blocked:
            self.assertIn('awaiting a verdict', entry['reason'])
            self.assertNotIn('no valid revision', entry['reason'])

    def test_a_project_already_serving_its_newest_revision_is_servable(self):
        self.stage_and_validate()
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        activation.activate_revision(project.revisions.get())

        # Its only revision is active rather than valid, so a status test alone would refuse it.
        self.assertEqual(project.revisions.get().status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(cutover.unservable_projects(self.migration), [])

    def test_a_project_serving_an_older_revision_is_servable(self):
        # It keeps serving that revision across the fence, which is what the guard asks.
        self.stage_and_validate()
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        activation.activate_revision(project.revisions.get())
        CustomScriptProjectRevision.objects.create(
            project=project, digest='b' * 64, status=RevisionStatusChoices.INVALID
        )

        project.refresh_from_db()
        self.assertNotEqual(project.active_revision_id, project.revisions.order_by('-created').first().pk)
        self.assertEqual(cutover.unservable_projects(self.migration), [])

    def test_a_module_staging_never_covered_stops_the_fence(self):
        self.stage_and_validate()
        # Added after staging, so the map names a project key no Project row answers to.
        self.legacy_synced_module('reporting/audit.py', SYNCED_SCRIPT)
        added = next(key for key in mapping.project_keys(mapping.build_map()) if key.startswith('reporting'))

        with self.assertRaises(cutover.CutoverRefused) as caught:
            cutover.enter_cutover(self.migration)

        self.assertIn(added, str(caught.exception))
        self.assertIn('has not been staged', str(caught.exception))

    def test_the_fence_crosses_once_every_project_can_be_served(self):
        self.stage_and_validate()

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.state, MigrationStateChoices.CUTOVER)
        self.assertTrue(self.migration.step_done(cutover.STEP))

    def test_a_resumed_crossing_is_not_refused_by_the_guard(self):
        # complete_step() is the last statement, so a crash before it leaves the closures done and
        # the map frozen. Re-checking servability there would refuse a fence already half crossed.
        self.stage_and_validate()
        cutover.enter_cutover(self.migration)
        self.crash_after_closing(MigrationStateChoices.CUTOVER)
        self.refuse_project(RevisionStatusChoices.INVALID)

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertTrue(self.migration.step_done(cutover.STEP))

    def test_a_crossing_that_closed_without_advancing_is_not_refused(self):
        # advance() runs after the closures, so this state is reachable too, and the frozen map is
        # what tells the two apart rather than the state.
        self.stage_and_validate()
        cutover.enter_cutover(self.migration)
        self.crash_after_closing(MigrationStateChoices.STAGING)
        self.refuse_project(RevisionStatusChoices.INVALID)

        cutover.enter_cutover(self.migration)

        self.migration.refresh_from_db()
        self.assertEqual(self.migration.state, MigrationStateChoices.CUTOVER)
        self.assertTrue(self.migration.step_done(cutover.STEP))
