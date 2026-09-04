import json
import tempfile
import uuid
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import django_rq
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings

from core.choices import JobNotificationChoices, JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectChange
from core.signals import clear_events
from extras.choices import LogLevelChoices
from extras.models import Tag
from netbox.context import current_request
from netbox_scripts.activation import activate_revision, deactivate_revision
from netbox_scripts.execution import ScriptNotExecutableError, load_script_class, run_script
from netbox_scripts.jobs import CustomScriptJob
from netbox_scripts.models import (
    CustomScript,
    ScriptFile,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.runtime.exceptions import LocalCacheError, ScriptResolutionError
from netbox_scripts.runtime.naming import PRIVATE_ROOT, revision_module_name
from netbox_scripts.scripts import AbortScript, Script
from netbox_scripts.storage import service
from netbox_scripts.tests.runtime.test_cache import discard_tree
from netbox_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_scripts.tests.test_branching import branching_installed, fake_branching, fake_contextvars
from netbox_scripts.validation import validate_revision
from utilities.datetime import local_now
from utilities.exceptions import AbortScript as LegacyAbortScript
from utilities.request import NetBoxFakeRequest


class Creating(Script):
    """Creates one tag, so a caller can see whether the run committed."""

    def run(self, data, commit):
        Tag.objects.create(name='Created By Script', slug='created-by-script')
        self.saw_commit = commit
        self.log_success('The tag was created.')
        return 'created'


class Aborting(Script):
    """Aborts through this plugin's own AbortScript."""

    def run(self, data, commit):
        raise AbortScript('Nothing left to do.')


class AbortingLegacy(Script):
    """Aborts through the abort exception a script written for the built-in runner raises."""

    def run(self, data, commit):
        raise LegacyAbortScript('Nothing left to do.')


class Failing(Script):
    """Raises an ordinary exception."""

    def run(self, data, commit):
        raise ValueError('the value was wrong')


def fake_request(user):
    """
    Return the shape a queued run carries, which is what copy_safe_request() produces.

    The user is not decoration. Event tracking makes this the current request, and change
    logging then reads the user off it for every object the script touches.
    """
    return NetBoxFakeRequest(
        {
            'META': {},
            'COOKIES': {},
            'POST': {},
            'GET': {},
            'FILES': {},
            'user': user,
            'method': 'POST',
            'path': '/plugins/netbox-scripts/scripts/1/run/',
            'id': uuid.uuid4(),
        }
    )


@contextmanager
def unroutable_probe(*, branch=None):
    """Install branching with the probe model no longer branch-aware, and a branch set or not."""
    # No deactivate_branch on the fake, which is the one state main_schema_only() leaves a branch
    # active in, and therefore the only way a run reaches _execute inside one.
    modules = {**fake_branching(supports_branching=lambda model: False), **fake_contextvars(branch)}
    with branching_installed(modules):
        yield


class ExecutionTestMixin:
    def run_one(self, script_class, *, commit=True, request=None):
        instance = script_class()
        run_script(instance, data={}, commit=commit, request=request)
        return instance

    def levels(self, instance):
        return [record['status'] for record in instance.messages]

    def messages(self, instance):
        return '\n'.join(record['message'] for record in instance.messages)


class CommitBehaviourTestCase(ExecutionTestMixin, TestCase):
    def test_a_committed_run_persists_what_the_script_created(self):
        instance = self.run_one(Creating, commit=True)

        self.assertTrue(Tag.objects.filter(slug='created-by-script').exists())
        self.assertEqual(instance.output, 'created')
        self.assertFalse(instance.failed)

    def test_a_dry_run_leaves_the_database_unchanged(self):
        self.run_one(Creating, commit=False)

        self.assertFalse(Tag.objects.filter(slug='created-by-script').exists())

    def test_a_dry_run_still_hands_the_script_the_commit_flag(self):
        instance = self.run_one(Creating, commit=False)

        self.assertIs(instance.saw_commit, False)

    def test_a_dry_run_still_records_the_log_and_the_output(self):
        instance = self.run_one(Creating, commit=False)

        self.assertEqual(instance.output, 'created')
        self.assertIn('The tag was created.', self.messages(instance))
        self.assertFalse(instance.failed)

    def test_a_dry_run_says_the_changes_were_reverted(self):
        instance = self.run_one(Creating, commit=False)

        self.assertIn('reverted', self.messages(instance))
        # A reverted dry run is not a failed run.
        self.assertFalse(instance.failed)

    def test_a_committed_run_does_not_claim_anything_was_reverted(self):
        instance = self.run_one(Creating, commit=True)

        self.assertNotIn('reverted', self.messages(instance))


class AbortTestCase(ExecutionTestMixin, TestCase):
    def test_an_abort_is_logged_as_a_failure_and_propagates(self):
        instance = Aborting()

        with self.assertRaises(AbortScript):
            run_script(instance, data={}, commit=True)

        self.assertTrue(instance.failed)
        self.assertIn('Nothing left to do.', self.messages(instance))

    def test_an_abort_carries_no_traceback(self):
        instance = Aborting()

        with self.assertRaises(AbortScript):
            run_script(instance, data={}, commit=True)

        self.assertNotIn('Traceback', self.messages(instance))

    def test_the_legacy_abort_is_treated_the_same_way(self):
        # The built-in runner matches its own abort with an exact type check, so the two classes
        # are not interchangeable. A script carried over unchanged raises the other one.
        instance = AbortingLegacy()

        with self.assertRaises(LegacyAbortScript):
            run_script(instance, data={}, commit=True)

        self.assertTrue(instance.failed)
        self.assertIn('Nothing left to do.', self.messages(instance))
        self.assertNotIn('Traceback', self.messages(instance))


class FailureTestCase(ExecutionTestMixin, TestCase):
    def test_an_unexpected_exception_records_its_type_and_traceback(self):
        instance = Failing()

        with self.assertRaises(ValueError):
            run_script(instance, data={}, commit=True)

        self.assertTrue(instance.failed)
        logged = self.messages(instance)
        self.assertIn('ValueError', logged)
        self.assertIn('the value was wrong', logged)
        self.assertIn('Traceback', logged)

    def test_an_unexpected_exception_reverts_what_the_script_wrote(self):
        class PartiallyFailing(Script):
            def run(self, data, commit):
                Tag.objects.create(name='Half Done', slug='half-done')
                raise ValueError('too late')

        with self.assertRaises(ValueError):
            run_script(PartiallyFailing(), data={}, commit=True)

        self.assertFalse(Tag.objects.filter(slug='half-done').exists())

    def test_a_failed_run_says_the_changes_were_reverted(self):
        instance = Failing()

        with self.assertRaises(ValueError):
            run_script(instance, data={}, commit=True)

        self.assertIn('reverted', self.messages(instance))


class RequestProcessorTestCase(ExecutionTestMixin, TestCase):
    def processors(self, entered):
        """Return two sentinel processors, the second standing in for event tracking."""

        def plain(request):
            @contextmanager
            def _entered():
                entered.append('plain')
                yield

            return _entered()

        def tracking(request):
            @contextmanager
            def _entered():
                entered.append('tracking')
                yield

            return _entered()

        return plain, tracking

    def run_with_processors(self, entered, *, commit):
        plain, tracking = self.processors(entered)
        with (
            patch('netbox_scripts.execution.event_tracking', new=tracking),
            patch.dict('netbox.registry.registry', {'request_processors': [plain, tracking]}, clear=False),
        ):
            run_script(Creating(), data={}, commit=commit, request=None)

    def test_every_processor_runs_for_a_committed_run(self):
        entered = []

        self.run_with_processors(entered, commit=True)

        self.assertEqual(entered, ['plain', 'tracking'])

    def test_event_tracking_is_skipped_for_a_dry_run(self):
        entered = []

        self.run_with_processors(entered, commit=False)

        # A dry run rolls back, so queueing events only to discard them would be wasted work,
        # and any that escaped the discard would describe changes that never happened.
        self.assertEqual(entered, ['plain'])

    def test_a_processor_that_cannot_start_does_not_end_the_run(self):
        entered = []
        plain, tracking = self.processors(entered)

        def broken(request):
            raise RuntimeError('this processor belongs to another plugin')

        instance = Creating()
        with (
            patch('netbox_scripts.execution.event_tracking', new=tracking),
            patch.dict('netbox.registry.registry', {'request_processors': [broken, plain]}, clear=False),
            self.assertLogs('netbox.plugins.netbox_scripts.execution', 'WARNING') as logged,
        ):
            run_script(instance, data={}, commit=True)

        self.assertEqual(entered, ['plain'])
        self.assertEqual(instance.output, 'created')
        self.assertIn('broken', logged.output[0])

    def test_event_tracking_failing_to_start_does_end_a_committed_run(self):
        def broken_tracking(request):
            raise RuntimeError('event tracking is unavailable')

        with (
            patch('netbox_scripts.execution.event_tracking', new=broken_tracking),
            patch.dict('netbox.registry.registry', {'request_processors': [broken_tracking]}, clear=False),
            self.assertRaises(RuntimeError),
        ):
            run_script(Creating(), data={}, commit=True)

        # A committed run that silently emitted no events would look successful and be wrong.
        self.assertFalse(Tag.objects.filter(slug='created-by-script').exists())


class RequestContextTestCase(ExecutionTestMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = get_user_model().objects.create_user(username='runner')
        self.received = []
        clear_events.connect(self.record, dispatch_uid='execution-test')
        self.addCleanup(clear_events.disconnect, self.record, dispatch_uid='execution-test')

    def record(self, sender, **kwargs):
        self.received.append(sender)

    def test_a_committed_run_attributes_its_changes_to_the_requesting_user(self):
        self.run_one(Creating, commit=True, request=fake_request(self.user))

        change = ObjectChange.objects.get(changed_object_id=Tag.objects.get(slug='created-by-script').pk)
        self.assertEqual(change.user, self.user)

    def test_pending_events_are_cleared_when_the_script_raises(self):
        request = fake_request(self.user)

        with self.assertRaises(ValueError):
            run_script(Failing(), data={}, commit=True, request=request)

        self.assertEqual(self.received, [request])

    def test_nothing_is_cleared_when_the_run_succeeds(self):
        self.run_one(Creating, commit=True, request=fake_request(self.user))

        self.assertEqual(self.received, [])

    def test_a_run_without_a_request_clears_nothing_and_still_raises(self):
        with self.assertRaises(ValueError):
            run_script(Failing(), data={}, commit=True, request=None)

        self.assertEqual(self.received, [])

    def test_a_failed_run_leaves_no_current_request_behind(self):
        # A worker outlives the run. Left set, this run's request would attribute every later
        # change in that process to a user who was not there.
        with self.assertRaises(ValueError):
            run_script(Failing(), data={}, commit=True, request=fake_request(self.user))

        self.assertIsNone(current_request.get())

    def test_a_run_nested_in_a_request_gives_that_request_back(self):
        outer = fake_request(self.user)
        current_request.set(outer)
        self.addCleanup(current_request.set, None)

        self.run_one(Creating, commit=True, request=fake_request(self.user))

        self.assertIs(current_request.get(), outer)


class ScriptJobTestMixin:
    """Builds a project that is actually serving a revision, so a script can be run."""

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.cache_root = Path(tempfile.mkdtemp())
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        self.user = get_user_model().objects.create_user(username='runner')
        self.project = ScriptProject.objects.create(name='Runnable', key='runnable')

    def _purge_namespace(self):
        import sys

        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def publish(self, files, entrypoints=('deploy.py',), activate=True):
        """Stage, validate and optionally activate one tree, returning the revision."""
        for path in entrypoints:
            ScriptFile.objects.get_or_create(project=self.project, source_path=path, defaults={'enabled': True})
        revision, _ = service.stage_revision(self.project, files)
        revision = validate_revision(revision, job=Job.objects.create(name='validation', job_id=uuid.uuid4()))
        if activate:
            activate_revision(revision)
            self.project.refresh_from_db()
        return revision

    def script(self):
        return CustomScript.objects.get(project=self.project)

    def run_job(self, script=None, *, data=None, commit=True, request=None, event=None):
        """Enqueue a run immediately and return the finished Job."""
        return CustomScriptJob.enqueue_run(
            script or self.script(),
            data=data if data is not None else {},
            commit=commit,
            request=request,
            user=self.user,
            event=event,
            immediate=True,
        )


MAKES_A_TAG = (
    b'from netbox_scripts.scripts import Script\n'
    b'from extras.models import Tag\n\n\n'
    b'class MakeTag(Script):\n'
    b'    class Meta:\n'
    b"        name = 'Make One Tag'\n\n"
    b'    def run(self, data, commit):\n'
    b"        Tag.objects.create(name='From Revision', slug='from-revision')\n"
    b"        self.log_success('The tag was created.')\n"
    b"        return 'done'\n"
)

RAISES = (
    b'from netbox_scripts.scripts import Script\n\n\n'
    b'class Boom(Script):\n'
    b'    def run(self, data, commit):\n'
    b"        raise RuntimeError('the script broke')\n"
)

# Returns what it was handed, so the binding is asserted through a real run rather than
# by reaching into the instance.
REPORTS_ITS_EVENT = (
    b'from netbox_scripts.scripts import Script\n\n\n'
    b'class ReportEvent(Script):\n'
    b'    def run(self, data, commit):\n'
    b'        if self.event is None:\n'
    b"            return 'no event'\n"
    b"        return '{} {}'.format(self.event['event_type'], self.event['object_id'])\n"
)


class LoadScriptClassTestCase(ScriptJobTestMixin, TestCase):
    """What load_script_class refuses, and what it refuses to disclose while doing it."""

    def test_a_project_serving_nothing_is_refused_rather_than_dereferenced(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        deactivate_revision(self.project.active_revision)
        script.refresh_from_db()

        with self.assertRaises(ScriptResolutionError) as caught:
            load_script_class(script)

        self.assertEqual(caught.exception.code, 'not_serving')

    def test_a_cache_failure_keeps_its_paths_out_of_the_message(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        storage_key = str(self.project.storage_key)
        leaky = f'/var/cache/{storage_key}/{revision.digest}/deploy.py could not be read'

        with (
            patch('netbox_scripts.execution.resolve_script_class', side_effect=LocalCacheError(leaky)),
            self.assertRaises(ScriptResolutionError) as caught,
        ):
            load_script_class(script)

        message = str(caught.exception)
        self.assertNotIn(storage_key, message)
        self.assertNotIn(revision.digest, message)
        self.assertIn('could not be read', message)


class EnqueueRunTestCase(ScriptJobTestMixin, TestCase):
    def test_enqueue_pins_the_active_revision_on_the_job(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()

        job = CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)

        self.assertEqual(job.object, script)
        self.assertEqual(job.data['revision_id'], revision.pk)
        self.assertEqual(job.data['revision_digest'], revision.digest)
        self.assertEqual(job.data['module_path'], 'deploy')
        self.assertEqual(job.data['class_name'], 'MakeTag')
        self.assertIs(job.data['commit'], True)

    def test_enqueue_is_refused_for_a_script_that_cannot_run(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        script.enabled = False
        script.save()

        with self.assertRaises(ScriptNotExecutableError) as caught:
            CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)

        self.assertIn('It is disabled.', str(caught.exception))
        self.assertFalse(Job.objects.filter(object_id=script.pk).exists())

    def test_enqueue_is_refused_when_the_project_serves_no_revision(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        deactivate_revision(revision)
        script.refresh_from_db()

        with self.assertRaises(ScriptNotExecutableError) as caught:
            CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)

        # Deactivation retires the script too, so retirement is the condition that holds first.
        self.assertIn('It is retired', str(caught.exception))

    def test_the_notification_policy_comes_from_the_script_metadata(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()

        job = CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)

        self.assertEqual(job.notifications, script.notifications_default)

    def test_a_supplied_notification_policy_wins(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        job = CustomScriptJob.enqueue_run(
            self.script(),
            data={},
            commit=True,
            user=self.user,
            notifications=JobNotificationChoices.NOTIFICATION_ON_FAILURE,
        )

        self.assertEqual(job.notifications, JobNotificationChoices.NOTIFICATION_ON_FAILURE)

    def test_an_event_payload_is_recorded_on_the_job(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        event = {'event_type': 'object_created', 'object_type': 'dcim.device', 'object_id': 7}

        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user, event=event)

        self.assertEqual(job.data['event'], event)

    def test_a_run_no_event_drove_records_none(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)

        self.assertIsNone(job.data['event'])

    def test_an_immediate_run_never_reaches_the_queue(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        queue = django_rq.get_queue('default')
        queue.empty()
        self.addCleanup(queue.empty)

        # Core queues every non-immediate job from a transaction.on_commit hook, so an
        # implementation reaching that branch would hand the finished run to a worker as well.
        with self.captureOnCommitCallbacks(execute=True):
            job = self.run_job()

        self.assertEqual(queue.count, 0)
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(Tag.objects.filter(slug='from-revision').count(), 1)

    def test_an_immediate_run_starts_from_a_pinned_row(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        seen = {}

        # The pin has to be on the row before the handler starts. Snapshot it there rather
        # than reading call_args later, which holds a reference the pin write mutates.
        def capture(job, *args, **kwargs):
            row = Job.objects.get(pk=job.pk)
            seen['data'] = row.data or {}
            seen['status'] = row.status

        with patch.object(CustomScriptJob, 'handle', side_effect=capture):
            self.run_job()

        self.assertEqual(seen['data'].get('revision_id'), revision.pk)
        self.assertEqual(seen['data'].get('revision_digest'), revision.digest)
        self.assertEqual(seen['status'], JobStatusChoices.STATUS_PENDING)

    def test_an_immediate_row_matches_a_queued_row(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        # The immediate branch builds its own row, so parity with the queued construction is
        # pinned rather than trusted.
        queued = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)
        with patch.object(CustomScriptJob, 'handle'):
            immediate = self.run_job()

        own = {'id', 'job_id', 'created'}
        for field in (f.name for f in Job._meta.concrete_fields if f.name not in own):
            self.assertEqual(getattr(immediate, field), getattr(queued, field), field)

    def test_an_interrupted_immediate_run_leaves_a_terminated_row(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        # handle() terminates the row for every Exception, so only a torn-down process reaches
        # this path. It starts the row first, which is the state a stopped run is caught in.
        def interrupt(job, *args, **kwargs):
            job.start()
            raise KeyboardInterrupt

        with patch.object(CustomScriptJob, 'handle', side_effect=interrupt), self.assertRaises(KeyboardInterrupt):
            self.run_job()

        (job,) = Job.objects.filter(object_id=self.script().pk)
        self.assertEqual(job.status, JobStatusChoices.STATUS_ERRORED)
        self.assertEqual(job.error, 'The run was interrupted.')
        self.assertIsNotNone(job.completed)
        self.assertEqual(job.data['revision_id'], self.project.active_revision.pk)

    def test_an_immediate_recurrence_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        for deferral in ({'interval': 60}, {'schedule_at': local_now() + timedelta(hours=1)}):
            with self.subTest(**deferral), self.assertRaises(ValueError):
                CustomScriptJob.enqueue_run(
                    self.script(), data={}, commit=True, user=self.user, immediate=True, **deferral
                )

        self.assertFalse(Job.objects.filter(object_id=self.script().pk).exists())

    def test_a_declared_timeout_reaches_the_queued_run(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        script.job_timeout_override = 300
        script.save()

        with patch.object(CustomScriptJob, 'enqueue', wraps=CustomScriptJob.enqueue) as enqueue:
            CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)

        self.assertEqual(enqueue.call_args.kwargs['job_timeout'], 300)

    def test_an_immediate_run_discards_the_declared_timeout(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        script.job_timeout_override = 300
        script.save()
        seen = {}

        with patch.object(CustomScriptJob, 'run', lambda runner, **kwargs: seen.update(kwargs)):
            CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user, immediate=True)

        self.assertEqual(seen['module_path'], 'deploy')
        self.assertNotIn('job_timeout', seen)

    def test_an_immediate_run_inside_an_open_transaction_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        with self.assertRaises(RuntimeError), transaction.atomic():
            CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user, immediate=True)

        self.assertFalse(Job.objects.filter(object_id=self.script().pk).exists())


# TransactionTestCase: a second session can only observe rows this one has committed, and under
# TestCase the save is a savepoint a real commit is indistinguishable from.
class ImmediateRunCommitTestCase(ScriptJobTestMixin, TransactionTestCase):
    """The immediate run's Job row is really committed before the script executes."""

    @staticmethod
    def visible_to_another_session(pk):
        """Report whether a separate session can see one Job row, which only a commit allows."""
        connection = connections.create_connection(DEFAULT_DB_ALIAS)
        connection.ensure_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT COUNT(*) FROM core_job WHERE id = %s', (pk,))
                return cursor.fetchone()[0] == 1
        finally:
            connection.close()

    def test_the_row_is_visible_to_another_session_while_the_run_executes(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        observed = {}

        def observe(runner, **kwargs):
            observed['visible'] = self.visible_to_another_session(runner.job.pk)

        with patch.object(CustomScriptJob, 'run', observe):
            job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=False, user=self.user, immediate=True)

        self.assertTrue(observed['visible'])
        self.assertTrue(self.visible_to_another_session(job.pk))


class ScheduledRunTestCase(ScriptJobTestMixin, TestCase):
    """
    Deferred and recurring runs, and which revision each of them executes.

    A one-shot run pins at enqueue, so it executes the source the operator was looking at. A
    recurring run pins nothing, because JobRunner.handle() re-enqueues a periodic job with the
    same kwargs, and a pin carried forward would run one frozen revision forever.
    """

    def test_a_deferred_run_reaches_the_worker_with_its_scheduled_time(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        when = local_now() + timedelta(hours=1)

        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user, schedule_at=when)

        self.assertEqual(job.scheduled, when)
        self.assertEqual(job.status, JobStatusChoices.STATUS_SCHEDULED)

    def test_a_deferred_run_still_pins_its_revision(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})

        job = CustomScriptJob.enqueue_run(
            self.script(), data={}, commit=True, user=self.user, schedule_at=local_now() + timedelta(hours=1)
        )

        self.assertEqual(job.data['revision_id'], revision.pk)

    def test_a_recurring_run_records_its_interval(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        job = CustomScriptJob.enqueue_run(
            self.script(),
            data={},
            commit=True,
            user=self.user,
            schedule_at=local_now() + timedelta(minutes=5),
            interval=60,
        )

        self.assertEqual(job.interval, 60)

    def test_a_recurring_run_pins_no_revision(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        job = CustomScriptJob.enqueue_run(
            self.script(),
            data={},
            commit=True,
            user=self.user,
            schedule_at=local_now() + timedelta(minutes=5),
            interval=60,
        )

        self.assertIsNone(job.data['revision_id'])
        self.assertIsNone(job.data['revision_digest'])

    def occurrence(self, job):
        """Run one occurrence of a recurring job's body and return the runner."""
        # The runner records its result on self.job without saving, because JobRunner.handle()
        # is what terminates and persists. Calling run() directly means reading it in memory.
        runner = CustomScriptJob(job)
        runner.run(
            revision_id=None,
            revision_digest=None,
            module_path='deploy',
            class_name='MakeTag',
            data={},
            commit=True,
        )
        return runner

    def test_a_recurring_occurrence_runs_the_revision_active_at_the_time(self):
        # The behaviour the absent pin exists to produce: a project that activates new source
        # between occurrences runs the new source on the next one.
        self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user, interval=60)
        replacement = self.publish({'deploy.py': MAKES_A_TAG.replace(b'From Revision', b'From Replacement')})

        runner = self.occurrence(job)

        self.assertEqual(runner.job.data['revision_digest'], replacement.digest)
        self.assertTrue(Tag.objects.filter(name='From Replacement').exists())

    def test_a_recurring_occurrence_fails_when_the_project_serves_nothing(self):
        # Failing loudly beats silently running whatever was last active.
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user, interval=60)
        deactivate_revision(revision)

        with self.assertRaises(JobFailed):
            self.occurrence(job)


class RunJobTestCase(ScriptJobTestMixin, TestCase):
    def test_a_run_executes_the_script_and_records_its_log(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})

        job = self.run_job()

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertTrue(Tag.objects.filter(slug='from-revision').exists())
        self.assertEqual(job.data['output'], 'done')
        self.assertEqual(job.data['revision_digest'], revision.digest)
        self.assertIn('The tag was created.', [record['message'] for record in job.data['log']])

    def test_a_dry_run_changes_nothing_and_still_records(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        job = self.run_job(commit=False)

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(Tag.objects.filter(slug='from-revision').exists())
        self.assertEqual(job.data['output'], 'done')

    def test_the_event_reaches_the_running_script(self):
        self.publish({'deploy.py': REPORTS_ITS_EVENT})

        job = self.run_job(event={'event_type': 'object_updated', 'object_id': 3})

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data['output'], 'object_updated 3')

    def test_a_script_nobody_triggered_sees_no_event(self):
        self.publish({'deploy.py': REPORTS_ITS_EVENT})

        job = self.run_job()

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data['output'], 'no event')

    def test_the_pinned_revision_runs_even_when_a_newer_one_is_active(self):
        first = self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)
        # A second revision replaces the first between the enqueue and the run.
        second = self.publish({'deploy.py': MAKES_A_TAG + b'\n# a later revision\n'})
        self.assertNotEqual(first.digest, second.digest)
        self.assertEqual(self.project.active_revision_id, second.pk)

        CustomScriptJob.handle(job, **job.data, data={}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data['revision_digest'], first.digest)

    def test_a_run_whose_revision_was_deleted_fails_the_job(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)
        payload = dict(job.data)
        ScriptProjectRevision.objects.filter(pk=revision.pk).delete()

        CustomScriptJob.handle(job, **payload, data={}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)

    def test_a_run_whose_class_is_no_longer_published_fails_the_job(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)
        payload = dict(job.data, class_name='Vanished')

        CustomScriptJob.handle(job, **payload, data={}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)

    def test_a_script_that_raises_fails_the_job_and_keeps_its_log(self):
        self.publish({'deploy.py': RAISES})

        job = self.run_job()

        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        logged = '\n'.join(record['message'] for record in job.data['log'])
        self.assertIn('RuntimeError', logged)
        self.assertIn('the script broke', logged)

    def test_the_job_log_carries_no_storage_identities(self):
        # An unhandled exception records its traceback, and the frame is a file in the runtime
        # cache, so the raw text names the storage key, the digest and the local cache path.
        self.publish({'deploy.py': RAISES})

        job = self.run_job()

        serialized = json.dumps(job.data) + str(job.log_entries)
        self.assertIn('the script broke', serialized)
        storage_key = str(self.project.storage_key)
        for private in (storage_key, uuid.UUID(storage_key).hex, PRIVATE_ROOT, str(self.cache_root)):
            self.assertNotIn(private, serialized)
        # The digest is deliberately not on that list. It is the pin, and a finished Job has to
        # say which revision it ran.
        self.assertEqual(job.data['revision_digest'], self.project.active_revision.digest)

    def test_the_revision_is_unloaded_after_a_run(self):
        import sys

        revision = self.publish({'deploy.py': MAKES_A_TAG})
        prefix = revision_module_name(str(self.project.storage_key), revision.digest)

        self.run_job()

        # Each run imports fresh, so module-level state cannot carry from one run to the next.
        self.assertFalse([name for name in sys.modules if name == prefix or name.startswith(f'{prefix}.')])

    def test_uploaded_files_reach_the_script_data(self):
        reads_a_file = (
            b'from netbox_scripts.scripts import Script\n\n\n'
            b'class ReadsFile(Script):\n'
            b'    def run(self, data, commit):\n'
            b"        return data['attachment'].read().decode()\n"
        )
        self.publish({'deploy.py': reads_a_file})
        request = fake_request(self.user)
        request.FILES = {'attachment': SimpleUploadedFile('notes.txt', b'file content')}

        job = self.run_job(request=request)

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data['output'], 'file content')

    def test_a_script_disabled_after_the_enqueue_does_not_run(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        job = CustomScriptJob.enqueue_run(script, data={}, commit=True, user=self.user)
        # Enabled is the administrator's field, so it has to reach a run already in the queue.
        CustomScript.objects.filter(pk=script.pk).update(enabled=False)

        CustomScriptJob.handle(job, **job.data, data={}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertFalse(Tag.objects.filter(slug='from-revision').exists())

    def test_a_deactivated_project_still_runs_a_pinned_revision(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        job = CustomScriptJob.enqueue_run(self.script(), data={}, commit=True, user=self.user)
        # Pinning means the run executes what was requested, so deactivation does not undo it.
        deactivate_revision(revision)

        CustomScriptJob.handle(job, **job.data, data={}, request=None)
        job.refresh_from_db()

        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(job.data['revision_digest'], revision.digest)


class RoutingProbeTestCase(ExecutionTestMixin, TestCase):
    """What a run does once the model it probes can no longer report the routing."""

    def test_a_run_still_inside_a_branch_is_refused(self):
        # Nothing this run wrote could be rolled back with the branch, so it must not start.
        with unroutable_probe(branch='fixing-hq'), self.assertRaises(ImproperlyConfigured) as refusal:
            self.run_one(Creating)

        self.assertIn('dcim.device', str(refusal.exception))
        self.assertIn('fixing-hq', str(refusal.exception))
        self.assertFalse(Tag.objects.filter(slug='created-by-script').exists())

    def test_a_run_outside_a_branch_says_so_in_its_log_and_still_commits(self):
        with unroutable_probe():
            instance = self.run_one(Creating)

        self.assertIn('dcim.device', self.messages(instance))
        self.assertIn(LogLevelChoices.LOG_WARNING, self.levels(instance))
        self.assertTrue(Tag.objects.filter(slug='created-by-script').exists())

    def test_an_ordinary_run_carries_no_such_warning(self):
        # A guard that fired here would put a line on every run of every script.
        instance = self.run_one(Creating)

        self.assertNotIn(LogLevelChoices.LOG_WARNING, self.levels(instance))
