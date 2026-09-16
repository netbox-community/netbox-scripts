import io
import uuid
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from core.choices import JobNotificationChoices, JobStatusChoices
from core.models import Job, ObjectChange
from extras.models import Tag
from netbox_scripts.management.commands.runcustomscript import Command
from netbox_scripts.models import NetBoxScript, ScriptProject
from netbox_scripts.runtime.exceptions import LocalCacheError, ScriptFileImportError
from netbox_scripts.tests.test_execution import MAKES_A_TAG, RAISES, ScriptJobTestMixin


class RunCustomScriptCommandTestCase(ScriptJobTestMixin, TestCase):
    """The shell route to one run: what it resolves, what it refuses, and what it reports."""

    def setUp(self):
        super().setUp()
        # Named nowhere below: the command falls back to the first superuser, so its presence
        # is what lets every run-it case leave --user out.
        get_user_model().objects.create_user(username='admin', is_superuser=True)

    def run_command(self, *args, **options):
        """Call the command with its output captured, returning what it wrote to stdout."""
        out = io.StringIO()
        call_command('runcustomscript', *args, stdout=out, stderr=io.StringIO(), **options)
        return out.getvalue()

    def test_a_bare_name_runs_the_script_and_commits(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        self.run_command('deploy.MakeTag', commit=True)

        self.assertTrue(Tag.objects.filter(slug='from-revision').exists())

    def test_a_run_without_commit_keeps_nothing(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        self.run_command('deploy.MakeTag')

        # Asserted alongside the absence, so this cannot pass because the run never happened.
        self.assertEqual(self.script().jobs.first().status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(Tag.objects.filter(slug='from-revision').exists())

    def test_the_project_qualified_name_resolves(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        self.run_command('runnable:deploy.MakeTag', commit=True)

        self.assertTrue(Tag.objects.filter(slug='from-revision').exists())

    def test_the_log_is_printed_at_the_requested_level(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        printed = self.run_command('deploy.MakeTag', commit=True)

        self.assertIn('The tag was created.', printed)

    def test_a_level_above_the_entry_leaves_it_out(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        printed = self.run_command('deploy.MakeTag', commit=True, loglevel='failure')

        self.assertNotIn('The tag was created.', printed)

    def test_a_script_that_raises_fails_the_command(self):
        self.publish({'deploy.py': RAISES})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.Boom')

        # A non-zero exit is the whole reason this exists rather than a REST call in a loop.
        self.assertIn('finished with status', str(caught.exception))
        self.assertNotIn('completed', str(caught.exception))

    def test_a_malformed_recorded_policy_fails_the_command_before_any_job(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        NetBoxScript.objects.filter(project=self.project).update(metadata={'notifications_default': 'not-a-policy'})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag')

        self.assertIn('execution settings', str(caught.exception))
        self.assertFalse(Job.objects.filter(object_id=self.script().pk).exists())

    def test_a_malformed_recorded_timeout_fails_the_command_even_though_it_would_be_dropped(self):
        # _run_now discards the timeout, so this run used to succeed on a value nothing could parse.
        self.publish({'deploy.py': MAKES_A_TAG})
        NetBoxScript.objects.filter(project=self.project).update(metadata={'job_timeout': 'not-a-duration'})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag')

        self.assertIn('job timeout', str(caught.exception))
        self.assertFalse(Job.objects.filter(object_id=self.script().pk).exists())

    def test_a_name_matching_nothing_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.Absent')

        self.assertIn('No Script matches', str(caught.exception))

    def test_a_name_without_a_class_is_refused(self):
        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy')

        self.assertIn('project:module.ClassName', str(caught.exception))

    def publish_second_project(self):
        """Publish the same tree into a second project, leaving both scripts runnable."""
        first = self.project
        self.project = ScriptProject.objects.create(name='Second', key='second')
        try:
            self.publish({'deploy.py': MAKES_A_TAG})
        finally:
            self.project = first

    def test_an_ambiguous_bare_name_names_the_candidates(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        # Both are executable, which is the only kind of ambiguity an operator can resolve.
        self.publish_second_project()

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag')

        message = str(caught.exception)
        self.assertIn('more than one', message)
        self.assertIn('runnable:deploy.MakeTag', message)
        self.assertIn('second:deploy.MakeTag', message)

    def test_data_that_is_not_json_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag', data='{not json')

        self.assertIn('not valid JSON', str(caught.exception))

    def test_data_that_is_not_an_object_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag', data='[1, 2]')

        self.assertIn('JSON object', str(caught.exception))

    def test_an_unknown_user_is_refused_rather_than_swapped(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag', user='nobody')

        # Deliberately unlike the built-in command, which silently ran as the first superuser.
        self.assertIn('No active user named', str(caught.exception))
        self.assertFalse(Tag.objects.filter(slug='from-revision').exists())

    def test_the_named_user_is_who_the_run_is_recorded_against(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        get_user_model().objects.create_user(username='operator')

        self.run_command('deploy.MakeTag', user='operator', commit=True)

        job = self.script().jobs.first()
        self.assertEqual(job.user.username, 'operator')

    def test_a_committed_run_is_change_logged_against_the_named_user(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        get_user_model().objects.create_user(username='operator')

        self.run_command('deploy.MakeTag', user='operator', commit=True)

        # A run with no request writes no ObjectChange at all, so this is what proves one.
        change = ObjectChange.objects.get(changed_object_id=Tag.objects.get(slug='from-revision').pk)
        self.assertEqual(change.user.username, 'operator')

    def test_the_notification_setting_is_forwarded_to_the_job(self):
        self.publish({'deploy.py': MAKES_A_TAG})

        self.run_command('deploy.MakeTag', commit=True, notifications=JobNotificationChoices.NOTIFICATION_NEVER)

        # Carried on a flag of its own rather than through --data, so an execution parameter
        # can never collide with a variable of the same name.
        self.assertEqual(self.script().jobs.first().notifications, JobNotificationChoices.NOTIFICATION_NEVER)

    def test_a_retired_sibling_does_not_make_a_bare_name_ambiguous(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        # Retirement never deletes the row, so without the executable check this name would
        # become permanently ambiguous the moment a second project was retired.
        retired = ScriptProject.objects.create(name='Old', key='old')
        NetBoxScript.objects.create(
            project=retired,
            module_path='deploy',
            class_name='MakeTag',
            display_name='Make One Tag',
            is_retired=True,
        )

        self.run_command('deploy.MakeTag', commit=True)

        self.assertTrue(Tag.objects.filter(slug='from-revision').exists())

    def test_an_inactive_user_is_refused(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        get_user_model().objects.create_user(username='departed', is_active=False)

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag', user='departed')

        self.assertIn('No active user named', str(caught.exception))

    def test_a_failure_raised_before_the_script_runs_still_reports_why(self):
        # The script's own log is empty for every failure reached before the class loads, so
        # the Job's log is the only place the reason exists.
        job = Job.objects.create(name='run', job_id=uuid.uuid4(), status=JobStatusChoices.STATUS_FAILED)
        job.log_entries = [{'level': 'error', 'message': 'The revision this run was pinned to no longer exists.'}]
        errors = io.StringIO()
        command = Command(stderr=errors)

        command.explain(job)

        self.assertIn('no longer exists', errors.getvalue())

    def test_a_load_failure_outside_the_narrow_set_is_reported_rather_than_raised(self):
        # Not rooted in StorageError, so the narrow tuple let it reach the operator as a traceback.
        self.publish({'deploy.py': MAKES_A_TAG})

        with (
            patch(
                'netbox_scripts.management.commands.runcustomscript.script_class_context',
                side_effect=ScriptFileImportError('deploy imports a module that is not there', {}),
            ),
            self.assertRaises(CommandError) as caught,
        ):
            self.run_command('deploy.MakeTag')

        self.assertIn('could not be loaded', str(caught.exception))

    def test_a_cache_failure_leaks_no_storage_identity_to_the_operator(self):
        revision = self.publish({'deploy.py': MAKES_A_TAG})
        key = str(self.project.storage_key)
        leaky = LocalCacheError(f'/var/cache/{key}/{revision.digest}/deploy.py could not be read')

        with (
            patch('netbox_scripts.execution.resolve_script_class', side_effect=leaky),
            self.assertRaises(CommandError) as caught,
        ):
            self.run_command('deploy.MakeTag')

        self.assertNotIn(key, str(caught.exception))
        self.assertNotIn(revision.digest, str(caught.exception))

    def test_a_retired_script_is_refused_before_its_source_is_loaded(self):
        self.publish({'deploy.py': MAKES_A_TAG})
        script = self.script()
        NetBoxScript.objects.filter(pk=script.pk).update(is_retired=True)

        with self.assertRaises(CommandError) as caught:
            self.run_command('deploy.MakeTag')

        self.assertIn('cannot be run', str(caught.exception))
        self.assertIn('It is retired', str(caught.exception))

    def test_ambiguity_checks_runnable_matches_beyond_the_display_limit(self):
        for index in range(7):
            self.project = ScriptProject.objects.create(name=f'Candidate {index}', key=f'candidate-{index}')
            self.publish({'deploy.py': MAKES_A_TAG})
            if index not in (0, 6):
                ScriptProject.objects.filter(pk=self.project.pk).update(enabled=False)
        with self.assertRaises(CommandError):
            Command().resolve('deploy.MakeTag')
