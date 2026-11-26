import ast
import re
import shutil
import tempfile
import uuid
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.choices import JobStatusChoices, ManagedFileRootPathChoices
from core.models import DataSource, Job
from extras.models import ScriptModule
from netbox_scripts.choices import (
    MigrationStateChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_scripts.constants import MIGRATION_GUIDE_URL
from netbox_scripts.jobs import (
    MigrationActivationJob,
    MigrationCleanupJob,
    MigrationCutoverJob,
    MigrationInventoryJob,
    MigrationReferencesJob,
    MigrationStagingJob,
    MigrationVerificationJob,
)
from netbox_scripts.migration import cutover, mapping, plan
from netbox_scripts.models import MigrationRun, ScriptProject, ScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin
from netbox_scripts.views import migration as migration_views
from utilities.testing import TestCase, create_test_user

GUIDE = Path(__file__).resolve().parents[3] / 'docs' / 'administration' / 'migration.md'
READY_BADGE = '<span class="badge text-bg-green">Ready</span>'
WARNING_BADGE = '<span class="badge text-bg-yellow">Warning</span>'
BLOCKING_BADGE = '<span class="badge text-bg-red">Blocking</span>'


class MigrationTriggerTestCase(ObjectPermissionTestMixin, TestCase):
    """The Migration page and the seven routes that queue a pass."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        # Job.enqueue() reaches the shared Redis, which a test run must not touch.
        self.inventory = self.enterContext(
            mock.patch.object(MigrationInventoryJob, 'enqueue', side_effect=self.fake_job)
        )
        self.staging = self.enterContext(mock.patch.object(MigrationStagingJob, 'enqueue', side_effect=self.fake_job))
        self.cutover = self.enterContext(mock.patch.object(MigrationCutoverJob, 'enqueue', side_effect=self.fake_job))
        self.activation = self.enterContext(
            mock.patch.object(MigrationActivationJob, 'enqueue', side_effect=self.fake_job)
        )
        self.references = self.enterContext(
            mock.patch.object(MigrationReferencesJob, 'enqueue', side_effect=self.fake_job)
        )
        self.cleanup = self.enterContext(mock.patch.object(MigrationCleanupJob, 'enqueue', side_effect=self.fake_job))
        self.verification = self.enterContext(
            mock.patch.object(MigrationVerificationJob, 'enqueue', side_effect=self.fake_job)
        )

    @staticmethod
    def fake_job(**kwargs):
        """Stand in for an enqueue, returning a Job row the view can redirect to."""
        return Job.objects.create(name='queued', job_id=uuid.uuid4(), user=kwargs.get('user'))

    def grant(self, *actions, constraints=None):
        return super().grant(ScriptProject, *actions, constraints=constraints)

    def grant_job_view(self, constraints=None):
        return super().grant(Job, 'view', constraints=constraints)

    @staticmethod
    def url(name):
        return reverse(f'plugins:netbox_scripts:{name}')

    def record(self, job_class, status=JobStatusChoices.STATUS_COMPLETED):
        return Job.objects.create(name=job_class.name, job_id=uuid.uuid4(), status=status)

    def closed_run(self):
        """A run the cleanup pass closed, whatever its Job went on to record."""
        return MigrationRun.objects.create(state=MigrationStateChoices.MIGRATED, completed=timezone.now())

    def proposed_directories(self, *directories):
        """Record an inventory Job proposing one Project per (Data Source, path) pair, keyed '<path>-proposed'."""
        job = self.record(MigrationInventoryJob)
        job.data = {
            'projects': [
                asdict(
                    plan.ProposedProject(
                        key=f'{data_path}-proposed',
                        name=data_path,
                        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                        data_source_id=data_source.pk,
                        data_path=data_path,
                        module_pks=(index,),
                    )
                )
                for index, (data_source, data_path) in enumerate(directories, start=1)
            ]
        }
        job.save()

    def reused(self):
        """Record both passes for a directory an operator's Project already held, so staging reused that Project."""
        data_source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        project = ScriptProject.objects.create(
            name='Existing automation',
            key='custom-project-key',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        revision = ScriptProjectRevision.objects.create(
            project=project, digest='e' * 64, status=RevisionStatusChoices.VALID
        )
        self.proposed_directories((data_source, 'automation'))
        staging = self.record(MigrationStagingJob)
        staging.data = {
            'projects': [
                {
                    'key': project.key,
                    'created': False,
                    'revision_pk': revision.pk,
                    'revision_created': True,
                    'revision_status': RevisionStatusChoices.MATERIALIZED,
                    'processing_queued': True,
                }
            ]
        }
        staging.save()
        return project, revision

    def test_the_page_needs_the_project_add_permission(self):
        self.assertHttpStatus(self.client.get(self.url('migration')), 403)
        self.grant('add')
        self.assertHttpStatus(self.client.get(self.url('migration')), 200)

    def test_the_page_queues_nothing(self):
        self.grant('add')
        self.client.get(self.url('migration'))
        self.inventory.assert_not_called()
        self.staging.assert_not_called()

    def test_the_page_names_the_latest_run_of_each(self):
        self.grant('add')
        self.grant_job_view()
        inventory = self.record(MigrationInventoryJob)
        response = self.client.get(self.url('migration'))
        body = response.content.decode()
        self.assertIn(inventory.get_absolute_url(), body)
        # Staging has never run, so its row says so rather than linking anywhere.
        self.assertIn('Never run', body)
        self.assertIn("A script at a Data Source's root makes the whole source one Project.", body)
        self.assertNotIn('prevent staging', body)

    def test_a_pass_left_running_points_at_the_recovery_on_the_page(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationStagingJob)
        self.assertNotIn('Stale running Jobs', self.client.get(self.url('migration')).content.decode())

        Job.objects.filter(pk=job.pk).update(status=JobStatusChoices.STATUS_RUNNING)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('Stale running Jobs', body)
        # The row already links the newest Job, so its blocker line adds only the pointer.
        self.assertEqual(body.count(job.get_absolute_url()), 1)

    def test_the_page_names_an_older_job_that_still_holds_a_pass(self):
        self.grant('add')
        self.grant_job_view()
        stale = self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        Job.objects.filter(pk=stale.pk).update(created=timezone.now() - timedelta(hours=1))
        finished = self.record(MigrationStagingJob)
        self.record(MigrationInventoryJob, status=JobStatusChoices.STATUS_RUNNING)

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertTrue(response.context['staging_queued'])
        self.assertEqual(response.context['staging_job'], finished)
        self.assertIn(stale.get_absolute_url(), body)
        # The Running inventory Job holds nothing back, so only the staging row points at the guide.
        self.assertEqual(body.count('Stale running Jobs'), 1)

    def test_a_pass_whose_newest_job_is_hidden_reads_not_visible_to_you(self):
        self.grant('add')
        jobs = {
            'inventory': self.record(MigrationInventoryJob),
            'staging': self.record(MigrationStagingJob),
            'cutover': self.record(MigrationCutoverJob),
            'activation': self.record(MigrationActivationJob),
            'references': self.record(MigrationReferencesJob),
            'cleanup': self.record(MigrationCleanupJob),
            'verification': self.record(MigrationVerificationJob),
        }

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual(response.context['hidden_jobs'], dict.fromkeys(jobs, True))
        for name, job in jobs.items():
            with self.subTest(name=name):
                self.assertIsNone(response.context[f'{name}_job'])
                self.assertNotIn(job.get_absolute_url(), body)
        self.assertEqual(body.count('Not visible to you'), len(jobs))
        self.assertIsNone(response.context['next_action'])

    def test_a_job_view_grant_that_excludes_migration_jobs_shows_none_of_them(self):
        self.grant('add')
        self.grant_job_view(constraints={'name': 'not-a-migration-job'})
        job = self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertTrue(response.context['hidden_jobs']['inventory'])
        self.assertNotIn(job.get_absolute_url(), body)
        self.assertNotIn('audit/legacy_report.py', body)
        self.assertIn('The last inventory recorded blocking findings, which must be resolved before staging.', body)
        self.assertIsNone(response.context['next_action'])

    def test_a_job_view_grant_shows_only_the_passes_it_covers(self):
        self.grant('add')
        self.grant_job_view(constraints={'name': MigrationInventoryJob.name})
        inventory = self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))
        staging = self.record(MigrationStagingJob)

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual(response.context['inventory_job'], inventory)
        self.assertIsNone(response.context['staging_job'])
        self.assertIn(inventory.get_absolute_url(), body)
        self.assertIn('audit/legacy_report.py', body)
        self.assertNotIn(staging.get_absolute_url(), body)
        self.assertEqual(body.count('Not visible to you'), 1)

    def test_an_older_job_the_user_may_view_does_not_stand_in_for_a_hidden_newer_one(self):
        self.grant('add')
        self.grant_job_view(constraints={'user': '$user'})
        older = self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))
        Job.objects.filter(pk=older.pk).update(user=self.user, created=timezone.now() - timedelta(hours=1))
        self.record(MigrationInventoryJob)
        self.assertHttpStatus(self.client.get(older.get_absolute_url()), 200)

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertIsNone(response.context['inventory_job'])
        self.assertTrue(response.context['hidden_jobs']['inventory'])
        self.assertNotIn(older.get_absolute_url(), body)
        self.assertNotIn('Staging will refuse', body)
        self.assertEqual(response.context['next_action'], 'stage')

    def test_a_running_job_the_user_cannot_view_is_not_named_on_the_page(self):
        self.grant('add')
        stale = self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        Job.objects.filter(pk=stale.pk).update(created=timezone.now() - timedelta(hours=1))
        self.record(MigrationStagingJob)

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertTrue(response.context['staging_queued'])
        self.assertEqual(response.context['staging_running'], [])
        self.assertNotIn(stale.get_absolute_url(), body)
        self.assertNotIn('Stale running Jobs', body)
        self.assertEqual(body.count('A Job for this pass is still queued or running.'), 1)

    def test_a_hidden_running_job_behind_a_visible_newer_one_still_explains_the_wait(self):
        self.grant('add')
        self.grant_job_view(constraints={'user': '$user'})
        stale = self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        Job.objects.filter(pk=stale.pk).update(created=timezone.now() - timedelta(hours=1))
        finished = self.record(MigrationStagingJob)
        Job.objects.filter(pk=finished.pk).update(user=self.user)

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual(response.context['staging_job'], finished)
        self.assertTrue(response.context['staging_queued'])
        self.assertEqual(response.context['staging_running'], [])
        self.assertNotIn(stale.get_absolute_url(), body)
        self.assertEqual(body.count('A Job for this pass is still queued or running.'), 1)

    def with_findings(self, *findings):
        """Record an inventory Job carrying the supplied findings."""
        job = self.record(MigrationInventoryJob)
        job.data = {'findings': list(findings)}
        job.save()
        return job

    @staticmethod
    def finding(level, code, path):
        return {'level': level, 'code': code, 'pk': 1, 'path': path, 'message': f'{path} is {code}.'}

    def test_a_blocking_finding_is_named_on_the_page(self):
        self.grant('add')
        self.grant_job_view()
        self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('Staging will refuse', body)
        self.assertIn('audit/legacy_report.py', body)
        self.assertIn('report_style', body)
        self.assertIn('audit/legacy_report.py is report_style.', body)
        # The explanation for a hidden inventory Job never joins the list a viewer gets.
        self.assertNotIn('The last inventory recorded blocking findings, which must be resolved before staging.', body)

    def test_every_blocking_finding_is_listed(self):
        self.grant('add')
        self.grant_job_view()
        self.with_findings(
            self.finding('blocking', 'report_style', 'one.py'),
            self.finding('blocking', 'not_importable', 'two-hyphen.py'),
        )
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('one.py', body)
        self.assertIn('two-hyphen.py', body)

    def test_a_warning_is_counted_rather_than_listed(self):
        self.grant('add')
        self.grant_job_view()
        self.with_findings(
            self.finding('warning', 'legacy_import', 'first.py'),
            self.finding('warning', 'legacy_import', 'second.py'),
        )
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)
        self.assertIn('2 modules import the legacy authoring API', body)
        # Deliberate: listing the paths is the inventory's business, not this page's.
        self.assertNotIn('first.py', body)

    def test_a_branch_import_warning_reaches_the_page(self):
        # It was downgraded from blocking so one uncertain branch could not stop every other
        # Project. A warning the page never renders would be the same as dropping it.
        self.grant('add')
        self.grant_job_view()
        self.with_findings(self.finding('warning', 'import_unresolvable_in_branch', 'optional.py'))
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)
        self.assertIn('only inside a conditional branch', body)

    def test_a_branch_import_warning_is_not_counted_as_a_legacy_import(self):
        self.grant('add')
        self.grant_job_view()
        self.with_findings(
            self.finding('warning', 'import_unresolvable_in_branch', 'optional.py'),
            self.finding('warning', 'legacy_import', 'old.py'),
        )
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('One module imports the legacy authoring API', body)
        self.assertIn('One module imports something this host cannot provide', body)

    def test_an_undefined_script_warning_reaches_the_page(self):
        self.grant('add')
        self.grant_job_view()
        self.with_findings(self.finding('warning', 'script_not_defined_here', 'deploy.py'))
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)
        self.assertIn('is not defined in the module it publishes from', body)

    @staticmethod
    def _dict_warning_code(node):
        """Return the code of a WARNING finding built as a dict literal, or None for anything else."""
        if not isinstance(node, ast.Dict):
            return None
        pairs = {
            key.value: value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        level, code = pairs.get('level'), pairs.get('code')
        if isinstance(level, ast.Name) and level.id == 'WARNING' and isinstance(code, ast.Constant):
            return code.value
        return None

    def test_every_warning_code_the_inventory_emits_is_counted_by_the_page(self):
        # Derived from the inventory's own source, never a list written here, because a hand-kept
        # list drifts in exactly the way that hid two of these codes until someone went looking.
        # Both shapes count: the _finding() helper, and the dict literals built beside it. Walking
        # only the helper is how reports_excluded stayed invisible to this guard.
        tree = ast.parse(Path(plan.__file__).read_text())
        emitted = {
            node.args[1].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == '_finding'
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == 'WARNING'
            and isinstance(node.args[1], ast.Constant)
        }
        emitted |= {code for code in map(self._dict_warning_code, ast.walk(tree)) if code}
        self.assertTrue(emitted)
        rendered = Path(migration_views.__file__).read_text()
        for code in sorted(emitted):
            with self.subTest(code=code):
                self.assertIn(f"code='{code}'", rendered)

    def test_a_clean_inventory_shows_no_refusal(self):
        self.grant('add')
        self.record(MigrationInventoryJob)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn('Staging will refuse', body)

    def test_a_job_carrying_no_report_renders(self):
        # An inventory that failed before it recorded anything leaves data as None.
        self.grant('add')
        job = self.record(MigrationInventoryJob, status=JobStatusChoices.STATUS_ERRORED)
        job.data = None
        job.save()
        self.assertHttpStatus(self.client.get(self.url('migration')), 200)

    def test_a_hidden_inventory_still_says_staging_will_refuse(self):
        self.grant('add')
        job = self.with_findings(
            self.finding('blocking', 'report_style', 'audit/legacy_report.py'),
            self.finding('warning', 'legacy_import', 'old.py'),
            self.finding('warning', 'publishes_nothing', 'helpers.py'),
            self.finding('warning', 'import_unresolvable_in_branch', 'optional.py'),
            self.finding('warning', 'script_not_defined_here', 'deploy.py'),
            self.finding('warning', 'reports_excluded', 'reports.py'),
        )
        job.data['projects'] = [{'key': 'audit', 'name': 'Audit'}]
        job.save()

        response = self.client.get(self.url('migration'))
        context = response.context

        self.assertTrue(context['staging_blocked'])
        self.assertEqual(context['blocking_findings'], [])
        self.assertEqual(context['rows'], [])
        self.assertEqual(
            [
                context[key]
                for key in ('warning_count', 'helper_count', 'branch_import_count', 'undefined_script_count')
            ],
            [0, 0, 0, 0],
        )
        self.assertFalse(context['reports_excluded'])
        self.assertIsNone(context['next_action'])
        body = response.content.decode()
        self.assertIn('The last inventory recorded blocking findings, which must be resolved before staging.', body)
        self.assertNotIn('audit/legacy_report.py', body)

    def inventoried(self, *keys):
        """Record an inventory Job proposing the named Projects, creating none of them."""
        job = self.record(MigrationInventoryJob)
        job.data = {'projects': [{'key': key, 'name': key.replace('-', ' ')} for key in keys]}
        job.save()
        return job

    def serving(self, *keys, activate=True):
        """Create one Project per key, serving a revision unless activate is False."""
        for key in keys:
            project = ScriptProject.objects.create(name=key, key=key)
            revision = ScriptProjectRevision.objects.create(
                project=project, digest='a' * 64, status=RevisionStatusChoices.ACTIVE
            )
            if not activate:
                continue
            # Set past clean(), which refuses a pointer the activation service did not move.
            ScriptProject.objects.filter(pk=project.pk).update(active_revision=revision)

    def page_query_count(self):
        """Render the Migration page and report how many queries it took."""
        with CaptureQueriesContext(connection) as captured:
            self.assertHttpStatus(self.client.get(self.url('migration')), 200)
        return len(captured)

    def test_the_page_does_not_query_once_per_project_row(self):
        # view as well: a row whose Project restrict() filters out never reaches the join at all.
        self.grant('add', 'view')
        self.grant_job_view()
        first = [f'project-{index}' for index in range(2)]
        self.serving(*first)
        self.inventoried(*first)
        two = self.page_query_count()

        rest = [f'project-{index}' for index in range(2, 10)]
        self.serving(*rest)
        self.inventoried(*first, *rest)

        self.assertEqual(self.page_query_count(), two)

    def test_the_page_does_not_query_per_row_before_activation(self):
        # Between staging and activation every Project has a null pointer, which is the state the
        # page spends most of its life in, and current_revision then falls back to a query.
        self.grant('add', 'view')
        self.grant_job_view()
        first = [f'pending-{index}' for index in range(2)]
        self.serving(*first, activate=False)
        self.inventoried(*first)
        two = self.page_query_count()

        rest = [f'pending-{index}' for index in range(2, 10)]
        self.serving(*rest, activate=False)
        self.inventoried(*first, *rest)

        self.assertEqual(self.page_query_count(), two)

    def test_the_page_does_not_query_once_per_proposed_directory(self):
        self.grant('add', 'view')
        self.grant_job_view()
        data_source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        directories = [f'directory-{index}' for index in range(10)]
        for data_path in directories:
            ScriptProject.objects.create(
                name=data_path,
                key=f'{data_path}-project',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=data_source,
                data_path=data_path,
            )
        pairs = [(data_source, data_path) for data_path in directories]
        self.proposed_directories(*pairs[:2])
        two = self.page_query_count()

        self.proposed_directories(*pairs)

        self.assertEqual(self.page_query_count(), two)

    def staged(self, status, recorded_status=None, scripts=()):
        """Create a Project with a revision, and the staging Job that reports having made it."""
        project = ScriptProject.objects.create(name='Staged Project', key='staged-project')
        revision = ScriptProjectRevision.objects.create(
            project=project, digest='f' * 64, status=status, discovered_scripts=list(scripts)
        )
        job = self.record(MigrationStagingJob)
        job.data = {
            'projects': [
                {
                    'key': project.key,
                    'created': True,
                    'revision_pk': revision.pk,
                    'revision_created': True,
                    'revision_status': recorded_status or status,
                }
            ]
        }
        job.save()
        return project, revision

    def test_a_staged_project_shows_its_revision_status_and_script_count(self):
        self.grant('add', 'view')
        self.grant_job_view()
        project, revision = self.staged(RevisionStatusChoices.ACTIVE, scripts=[{'class_name': 'NewIP'}])
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn(project.get_absolute_url(), body)
        self.assertIn(revision.get_absolute_url(), body)
        self.assertIn('f' * 12, body)
        self.assertIn('Active', body)

    def test_the_status_shown_is_the_verdict_not_what_staging_recorded(self):
        # The whole reason this reads live: staging records a status before validation runs.
        self.grant('add', 'view')
        self.grant_job_view()
        self.staged(RevisionStatusChoices.INVALID, recorded_status=RevisionStatusChoices.MATERIALIZED)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertIn('Invalid', body)
        self.assertNotIn('Materialized', body)

    def test_a_project_the_inventory_proposed_but_staging_has_not_made_is_listed(self):
        # Running the inventory without staging, or staging an older plan, both look like this.
        self.grant('add', 'view')
        self.grant_job_view()
        self.inventoried('not-staged-yet')
        response = self.client.get(self.url('migration'))
        self.assertEqual([row['key'] for row in response.context['rows']], ['not-staged-yet'])
        self.assertIsNone(response.context['rows'][0]['project'])
        self.assertIn('Not staged', response.content.decode())

    def test_both_passes_contribute_rows_without_duplicating_one(self):
        self.grant('add', 'view')
        self.grant_job_view()
        project, _revision = self.staged(RevisionStatusChoices.VALID)
        self.inventoried(project.key, 'not-staged-yet')
        rows = self.client.get(self.url('migration')).context['rows']
        self.assertEqual([row['key'] for row in rows], [project.key, 'not-staged-yet'])

    def test_the_table_is_absent_before_either_pass_has_run(self):
        self.grant('add', 'view')
        response = self.client.get(self.url('migration'))
        self.assertEqual(list(response.context['rows']), [])
        self.assertNotIn('Not staged', response.content.decode())

    def test_a_project_the_user_cannot_view_is_not_linked(self):
        # Gated by the project's own view permission, like every other revision surface.
        self.grant('add')
        self.grant_job_view()
        project, _revision = self.staged(RevisionStatusChoices.INVALID)
        body = self.client.get(self.url('migration')).content.decode()
        self.assertNotIn(project.get_absolute_url(), body)

    def test_a_project_the_user_cannot_view_is_not_reported_unstaged(self):
        self.grant('add')
        self.grant_job_view()
        project, _revision = self.staged(RevisionStatusChoices.VALID)
        self.inventoried('not-staged-yet')

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual(
            [(row['key'], row['project'], row['project_exists']) for row in response.context['rows']],
            [('not-staged-yet', None, False), (project.key, None, True)],
        )
        self.assertEqual(body.count('Not visible to you'), 1)
        self.assertEqual(body.count('Not staged'), 1)

    def test_a_project_without_a_stored_revision_is_not_reported_unstaged(self):
        self.grant('add', 'view')
        self.grant_job_view()
        project = ScriptProject.objects.create(name='Rejected Project', key='rejected-project')
        ScriptProjectRevision.objects.create(project=project, digest=None, status=RevisionStatusChoices.INVALID)
        self.inventoried(project.key, 'not-staged-yet')

        body = self.client.get(self.url('migration')).content.decode()

        self.assertIn(project.get_absolute_url(), body)
        self.assertEqual(body.count('No stored revision'), 1)
        self.assertEqual(body.count('Not staged'), 1)

    def test_a_constrained_view_grant_hides_only_the_projects_it_excludes(self):
        self.grant('add')
        self.grant('view', constraints={'key': 'visible-project'})
        self.grant_job_view()
        visible = ScriptProject.objects.create(name='Visible Project', key='visible-project')
        ScriptProject.objects.create(name='Hidden Project', key='hidden-project')
        self.inventoried('visible-project', 'hidden-project')

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual([row['project'] for row in response.context['rows']], [visible, None])
        self.assertEqual(body.count('No stored revision'), 1)
        self.assertEqual(body.count('Not visible to you'), 1)

    def test_hidden_staging_and_verification_jobs_contribute_no_rows_or_result(self):
        self.grant('add')
        self.staged(RevisionStatusChoices.VALID)
        self.verified({'status': 'blocking', 'checks': []})

        response = self.client.get(self.url('migration'))

        self.assertEqual(response.context['rows'], [])
        self.assertIsNone(response.context['verification_status'])
        self.assertNotIn('Verification result', response.content.decode())

    def test_a_visible_inventory_contributes_rows_while_hidden_staging_contributes_none(self):
        self.grant('add')
        self.grant_job_view(constraints={'name': MigrationInventoryJob.name})
        self.inventoried('proposed-project')
        staging = self.record(MigrationStagingJob)
        staging.data = {'projects': [{'key': 'staged-project'}]}
        staging.save()

        rows = self.client.get(self.url('migration')).context['rows']

        self.assertEqual([row['key'] for row in rows], ['proposed-project'])

    def test_visible_staging_contributes_rows_while_a_hidden_inventory_contributes_none(self):
        self.grant('add')
        self.grant_job_view(constraints={'name': MigrationStagingJob.name})
        self.inventoried('proposed-project')
        staging = self.record(MigrationStagingJob)
        staging.data = {'projects': [{'key': 'staged-project'}]}
        staging.save()

        rows = self.client.get(self.url('migration')).context['rows']

        self.assertEqual([row['key'] for row in rows], ['staged-project'])

    def test_a_project_staging_reused_is_listed_once_in_its_proposal_row(self):
        self.grant('add', 'view')
        self.grant_job_view()
        project, revision = self.reused()

        response = self.client.get(self.url('migration'))
        body = response.content.decode()

        self.assertEqual(
            [(row['key'], row['name'], row['project'], row['revision']) for row in response.context['rows']],
            [(project.key, 'automation', project, revision)],
        )
        self.assertIn(project.get_absolute_url(), body)
        self.assertIn(revision.get_absolute_url(), body)
        self.assertNotIn('Not staged', body)

    def test_a_proposal_shows_the_project_holding_its_directory_without_the_staging_job(self):
        self.grant('add', 'view')
        self.grant_job_view(constraints={'name': MigrationInventoryJob.name})
        project, _revision = self.reused()

        rows = self.client.get(self.url('migration')).context['rows']

        self.assertEqual(
            [(row['key'], row['name'], row['project']) for row in rows], [(project.key, 'automation', project)]
        )

    def test_a_reused_project_the_user_cannot_view_is_not_reported_unstaged(self):
        self.grant('add')
        self.grant_job_view(constraints={'name': MigrationInventoryJob.name})
        project, _revision = self.reused()

        response = self.client.get(self.url('migration'))

        self.assertEqual(
            [(row['key'], row['name'], row['project'], row['project_exists']) for row in response.context['rows']],
            [(project.key, 'automation', None, True)],
        )
        self.assertNotIn('Not staged', response.content.decode())

    def test_a_proposal_takes_only_the_project_holding_its_exact_directory(self):
        self.grant('add', 'view')
        self.grant_job_view()
        automation = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        other = DataSource.objects.create(name='Other', type='local', source_url='file:///tmp/other')
        # Staging reused this Project before an operator moved it to another directory.
        moved = ScriptProject.objects.create(
            name='Moved automation',
            key='custom-project-key',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=automation,
            data_path='elsewhere',
        )
        ScriptProject.objects.create(
            name='Other automation',
            key='other-automation',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=other,
            data_path='automation',
        )
        # A proposal on each source lets both Projects past the two __in filters, so only the exact pair excludes them.
        self.proposed_directories((automation, 'automation'), (other, 'elsewhere'))
        staging = self.record(MigrationStagingJob)
        staging.data = {'projects': [{'key': moved.key}]}
        staging.save()

        response = self.client.get(self.url('migration'))

        self.assertEqual(
            [(row['key'], row['project'], row['project_exists']) for row in response.context['rows']],
            [('automation-proposed', None, False), ('elsewhere-proposed', None, False), (moved.key, moved, True)],
        )
        self.assertEqual(response.content.decode().count('Not staged'), 2)

    def test_a_refusal_stays_in_the_row_of_the_project_holding_its_directory(self):
        self.grant('add', 'view')
        self.grant_job_view()
        project, _revision = self.reused()
        other = DataSource.objects.create(name='Other', type='local', source_url='file:///tmp/other')
        # An earlier pass staged this Project under the proposal's key before an operator moved it away.
        ScriptProject.objects.create(
            name='Moved automation',
            key='automation-proposed',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=other,
            data_path='elsewhere',
        )
        Job.objects.filter(name=MigrationStagingJob.name).update(created=timezone.now() - timedelta(hours=1))
        # A later pass refused the proposal, and staging records a refusal under the proposal's key.
        refusal = self.record(MigrationStagingJob)
        refusal.data = {
            'projects': [
                {'key': 'automation-proposed', 'created': False, 'refused': 'This path collides with a script file.'}
            ]
        }
        refusal.save()

        response = self.client.get(self.url('migration'))

        self.assertEqual([(row['key'], row['project']) for row in response.context['rows']], [(project.key, project)])
        self.assertNotIn('Not staged', response.content.decode())

    def test_a_project_staged_under_the_proposal_key_keeps_its_row_after_moving_away(self):
        self.grant('add', 'view')
        self.grant_job_view()
        automation = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        other = DataSource.objects.create(name='Other', type='local', source_url='file:///tmp/other')
        # Staging created this Project for the proposal before an operator moved it to another Data Source.
        moved = ScriptProject.objects.create(
            name='Moved automation',
            key='automation-proposed',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=other,
            data_path='elsewhere',
        )
        holder = ScriptProject.objects.create(
            name='New automation',
            key='new-automation',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=automation,
            data_path='automation',
        )
        self.proposed_directories((automation, 'automation'))
        staging = self.record(MigrationStagingJob)
        staging.data = {'projects': [{'key': moved.key, 'created': True}]}
        staging.save()

        rows = self.client.get(self.url('migration')).context['rows']

        self.assertEqual(
            [(row['key'], row['name'], row['project']) for row in rows],
            [(holder.key, 'automation', holder), (moved.key, moved.key, moved)],
        )

    def test_a_proposal_no_project_holds_is_listed_beside_the_project_carrying_its_key(self):
        self.grant('add', 'view')
        self.grant_job_view()
        data_source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        # Staging created this Project for the automation proposal before an operator moved it to provision.
        moved = ScriptProject.objects.create(
            name='Moved automation',
            key='automation-proposed',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='provision',
        )
        self.proposed_directories((data_source, 'automation'), (data_source, 'provision'))

        response = self.client.get(self.url('migration'))

        self.assertEqual(
            [(row['key'], row['name'], row['project'], row['project_exists']) for row in response.context['rows']],
            [('automation-proposed', 'automation', None, False), (moved.key, 'provision', moved, True)],
        )
        self.assertEqual(response.content.decode().count('Not staged'), 1)

    def test_a_deleted_project_staged_under_the_proposal_key_adds_no_row(self):
        self.grant('add', 'view')
        self.grant_job_view()
        data_source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        self.proposed_directories((data_source, 'automation'))
        staging = self.record(MigrationStagingJob)
        # Staging created a Project under the proposal's key, and it has been deleted since.
        staging.data = {'projects': [{'key': 'automation-proposed', 'created': True}]}
        staging.save()

        response = self.client.get(self.url('migration'))

        self.assertEqual(
            [(row['key'], row['project'], row['project_exists']) for row in response.context['rows']],
            [('automation-proposed', None, False)],
        )
        self.assertEqual(response.content.decode().count('Not staged'), 1)

    def test_running_the_inventory_queues_one_pass(self):
        self.grant('add')
        response = self.client.post(self.url('migration_inventory'))
        self.assertHttpStatus(response, 302)
        self.inventory.assert_called_once_with(user=self.user)
        # Back to the page, which names the run it just queued and links to it. The operator lands
        # where the state table and the other pass are, rather than one click away from both.
        self.assertEqual(response.url, self.url('migration'))

    def test_the_inventory_route_needs_the_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_inventory')), 403)
        self.inventory.assert_not_called()

    def test_the_staging_confirmation_queues_nothing(self):
        self.grant('add')
        response = self.client.get(self.url('migration_stage'))
        self.assertHttpStatus(response, 200)
        self.assertIn('Stage Projects', response.content.decode())
        self.staging.assert_not_called()

    def test_staging_queues_one_pass(self):
        self.grant('add')
        response = self.client.post(self.url('migration_stage'))
        self.assertHttpStatus(response, 302)
        self.staging.assert_called_once_with(user=self.user)
        self.assertEqual(response.url, self.url('migration'))

    def test_both_passes_return_to_the_same_place(self):
        # The already-queued refusal always came back here. Asserting the three together is what
        # stops one of them drifting off on its own.
        self.grant('add')
        queued = self.client.post(self.url('migration_inventory'))
        staged = self.client.post(self.url('migration_stage'))

        self.assertEqual(queued.url, self.url('migration'))
        self.assertEqual(staged.url, self.url('migration'))

    def test_staging_refuses_while_a_pass_is_already_queued(self):
        self.grant('add')
        self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_stage'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.staging.assert_not_called()

    def test_the_staging_route_needs_the_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_stage')), 403)
        self.staging.assert_not_called()

    def open_run(self, state):
        """Open a migration run in one state, as a pass would have left it."""
        return MigrationRun.objects.create(state=state)

    def test_the_cutover_button_appears_only_once_a_run_is_staged(self):
        self.grant('add', 'migrate')
        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        self.open_run(MigrationStateChoices.STAGING)

        self.assertIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_button_names_what_it_would_refuse(self):
        # Withholding the button without saying why is the silence this page exists to remove.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)
        blocked = [{'project_key': 'automation', 'reason': 'holds no revision'}]

        with mock.patch.object(cutover, 'unservable_projects', return_value=blocked) as guard:
            body = self.client.get(self.url('migration')).content.decode()

        guard.assert_called_once()
        self.assertNotIn('Enter cutover', body)
        self.assertIn('automation', body)
        self.assertIn('holds no revision', body)

    def test_the_page_renders_a_reason_the_guard_itself_produced(self):
        # The whole path rather than a stand-in: a real built-in module, the real guard, the
        # real template.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)
        self.legacy_module()

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Enter cutover', body)
        self.assertIn('has not been staged', body)

    def legacy_module(self):
        """Create one built-in script module, which is what gives the map a Project key to name."""
        scripts_root = tempfile.mkdtemp(prefix='legacy-scripts-')
        self.addCleanup(shutil.rmtree, scripts_root, ignore_errors=True)
        self.enterContext(
            override_settings(
                STORAGES={
                    **settings.STORAGES,
                    'scripts': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': scripts_root, 'allow_overwrite': True},
                    },
                }
            )
        )
        storages['scripts'].save('audit.py', ContentFile(b'VALUE = 1\n'))
        return ScriptModule.objects.create(file_path='audit.py')

    def staged_fence(self):
        """Return a run holding a real frozen map with activation recorded, as a fence leaves it."""
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.legacy_module()
        run.record_journal(mapping=mapping.build_map())
        # The fence refuses unless every mapped Project was staged, so one that crossed leaves
        # the rows behind, serving nothing until an activation succeeds.
        for key in mapping.project_keys(run.journal['mapping']):
            ScriptProject.objects.create(name=key, key=key, source_type=ProjectSourceTypeChoices.UPLOAD)
        run.record_step(cutover.STEP, counts={})
        run.record_step(cutover.ACTIVATE_STEP, projects=[])
        return run

    def test_the_references_button_names_what_it_would_refuse(self):
        # Withholding it without saying why reproduces the silence this page exists to remove.
        self.grant('add', 'migrate')
        self.staged_fence()

        with mock.patch.object(cutover, 'projects_not_serving', return_value=['automation']) as guard:
            body = self.client.get(self.url('migration')).content.decode()

        guard.assert_called_once()
        self.assertNotIn('Repoint references', body)
        self.assertIn('automation', body)

    def test_the_references_button_is_withheld_by_the_guard_itself(self):
        # The whole path: a real map, the real predicate, the real template.
        self.grant('add', 'migrate')
        run = self.staged_fence()
        expected = mapping.project_keys(run.journal['mapping'])

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Repoint references', body)
        for key in expected:
            self.assertIn(key, body)

    def test_the_references_button_returns_once_a_refused_project_is_deleted(self):
        # Deleting it is the operator saying they do not want it, which must not wedge the rest.
        self.grant('add', 'migrate')
        run = self.staged_fence()
        self.assertNotIn('Repoint references', self.client.get(self.url('migration')).content.decode())

        ScriptProject.objects.filter(key__in=mapping.project_keys(run.journal['mapping'])).delete()

        self.assertIn('Repoint references', self.client.get(self.url('migration')).content.decode())

    def test_the_references_button_returns_once_every_project_serves(self):
        self.grant('add', 'migrate')
        self.staged_fence()

        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            body = self.client.get(self.url('migration')).content.decode()

        self.assertIn('Repoint references', body)

    def test_the_alert_is_silent_until_activation_has_run(self):
        # Nothing serves yet and the page already offers Activate Projects, so naming them
        # here would tell the operator to redo a step they have not taken.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.legacy_module()
        run.record_journal(mapping=mapping.build_map())
        run.record_step(cutover.STEP, counts={})

        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('The reference pass will refuse', body)
        self.assertIn('Activate Projects', body)

    def test_the_page_does_not_ask_before_the_fence_has_captured(self):
        # The predicate reads the frozen map, so asking without one would raise rather than refuse.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER)

        with mock.patch.object(cutover, 'projects_not_serving') as guard:
            self.client.get(self.url('migration'))

        guard.assert_not_called()

    def test_the_page_does_not_ask_what_a_crossed_fence_would_refuse(self):
        # The check reads the built-in rows, so it runs only where the button could render. That
        # is the recorded step rather than the state, since the button stays up mid-crossing.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER).record_step(cutover.STEP, counts={})

        with mock.patch.object(cutover, 'unservable_projects') as guard:
            self.client.get(self.url('migration'))

        guard.assert_not_called()

    def test_the_cutover_button_is_gone_once_the_fence_has_been_crossed(self):
        # The page must not offer a step the job would return from having done nothing.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER).record_step(cutover.STEP, counts={})

        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_is_still_offered_to_a_run_the_fence_left_mid_crossing(self):
        # Withholding the button here would strand the operator: staging refuses this state and
        # activation waits on the step record.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.CUTOVER)

        body = self.client.get(self.url('migration')).content.decode()

        self.assertIn('Enter cutover', body)
        self.assertNotIn('Activate Projects', body)

    def test_the_page_says_the_fence_may_have_fired(self):
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)

        self.assertIn('may already have closed', self.client.get(self.url('migration')).content.decode())

        run.record_step(cutover.STEP, counts={})

        self.assertNotIn('may already have closed', self.client.get(self.url('migration')).content.decode())

    def test_a_staged_run_is_not_described_as_mid_crossing(self):
        # The other half of the predicate: without it the notice would greet every staged run.
        self.grant('add', 'migrate')
        self.open_run(MigrationStateChoices.STAGING)

        self.assertNotIn('may already have closed', self.client.get(self.url('migration')).content.decode())

    def test_the_cutover_confirmation_queues_nothing(self):
        self.grant('add', 'migrate')
        response = self.client.get(self.url('migration_cutover'))
        self.assertHttpStatus(response, 200)
        self.assertIn('no way back', response.content.decode())
        self.cutover.assert_not_called()

    def test_the_cutover_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_cutover'), {'confirm': 'true', 'backup_taken': 'on'})
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.cutover.assert_called_once()

    def test_the_cutover_refuses_a_post_that_acknowledges_nothing(self):
        # Posted as a browser would, with the form's own marker but the box left unticked, so the
        # refusal is the acknowledgement and not ConfirmationForm's hidden field.
        self.grant('add', 'migrate')

        response = self.client.post(self.url('migration_cutover'), {'confirm': 'true'})

        self.assertHttpStatus(response, 200)
        self.cutover.assert_not_called()

    def test_the_cutover_confirmation_asks_for_the_backup(self):
        self.grant('add', 'migrate')

        body = self.client.get(self.url('migration_cutover')).content.decode()

        self.assertIn('backup_taken', body)
        self.assertIn('no way back', body)

    def test_the_cutover_confirmation_stays_silent_about_reports_when_there_are_none(self):
        self.grant('add', 'migrate')

        body = self.client.get(self.url('migration_cutover')).content.decode()

        self.assertNotIn('report module', body)

    def test_the_cutover_confirmation_names_the_reports_it_does_not_cover(self):
        self.grant('add', 'migrate')
        report = self.legacy_module()
        ScriptModule.objects.filter(pk=report.pk).update(file_root=ManagedFileRootPathChoices.REPORTS)

        body = self.client.get(self.url('migration_cutover')).content.decode()

        self.assertIn('one built-in report module', body)
        self.assertIn('does not cover', body)
        # The remedy, which has to be present however the mechanism is worded.
        self.assertIn('serve reports too', body)
        self.assertIn('recreate a report-only grant by hand', body)

    def test_the_cutover_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationCutoverJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_cutover'))
        self.assertHttpStatus(response, 302)
        self.cutover.assert_not_called()

    def test_the_cutover_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_cutover')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_cutover')), 403)
        self.cutover.assert_not_called()

    def test_the_page_names_the_latest_cutover(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationCutoverJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_a_staging_user_is_offered_none_of_the_steps_past_the_fence(self):
        # Every render condition is satisfied at each point, so the permission is all that keeps
        # them off. The crossing and the steps after it cannot render at once, so it takes two.
        self.grant('add')
        run = self.open_run(MigrationStateChoices.STAGING)

        self.assertNotIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})
        run.record_step('activate', projects=[])
        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Activate Projects', body)
        self.assertNotIn('Repoint references', body)

    def test_a_migrating_user_is_offered_every_step_the_state_allows(self):
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.STAGING)

        self.assertIn('Enter cutover', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})
        run.record_step('activate', projects=[])
        body = self.client.get(self.url('migration')).content.decode()

        self.assertNotIn('Enter cutover', body)
        self.assertIn('Activate Projects', body)
        self.assertIn('Repoint references', body)

    def test_the_activate_button_appears_only_once_the_fence_is_recorded(self):
        # Crossing the fence is what activation waits for, not merely reaching the cutover state:
        # a run whose cutover job failed partway has the state and not the step.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Activate Projects', self.client.get(self.url('migration')).content.decode())

        run.record_step('cutover', counts={})

        self.assertIn('Activate Projects', self.client.get(self.url('migration')).content.decode())

    def test_the_activate_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_activate'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.activation.assert_called_once()

    def test_the_activate_route_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationActivationJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_activate'))
        self.assertHttpStatus(response, 302)
        self.activation.assert_not_called()

    def test_the_activate_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_activate')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_activate')), 403)
        self.activation.assert_not_called()

    def test_the_page_names_the_latest_activation(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationActivationJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_the_repoint_button_appears_only_once_the_projects_are_activated(self):
        # The references name plugin rows, and activation is what creates them.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Repoint references', self.client.get(self.url('migration')).content.decode())

        run.record_step('activate', projects=[])

        self.assertIn('Repoint references', self.client.get(self.url('migration')).content.decode())

    def test_the_repoint_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_repoint'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.references.assert_called_once()

    def test_the_repoint_route_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationReferencesJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_repoint'))
        self.assertHttpStatus(response, 302)
        self.references.assert_not_called()

    def test_the_repoint_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_repoint')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_repoint')), 403)
        self.references.assert_not_called()

    def test_the_page_names_the_latest_reference_pass(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationReferencesJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def test_the_cleanup_button_waits_for_every_reference_step(self):
        # One finished step is not enough: a schedule needs the built-in rows still there.
        self.grant('add', 'migrate')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history'):
            run.record_step(step, counts={})
            self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

        run.record_step('recreate_schedules', counts={})

        self.assertIn('Clean up', self.client.get(self.url('migration')).content.decode())

    def test_the_cleanup_confirmation_queues_nothing(self):
        self.grant('add', 'migrate')
        response = self.client.get(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 200)
        self.assertIn('stored source', response.content.decode())
        self.cleanup.assert_not_called()

    def test_the_cleanup_confirmation_separates_retained_modules_from_blockers(self):
        self.grant('add', 'migrate')

        body = self.client.get(self.url('migration_cleanup')).content.decode()

        for phrase in (
            'module-level Job history or history for classes no longer published by their source are retained',
            'unless you have made a separate retention decision',
            'A module with waiting or running built-in Jobs is blocked, even when it also holds retained history',
            'Migration completes when no blockers remain, even if some modules are retained',
            'resolve blockers before retrying cleanup',
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, body)
        # The two false claims the page used to make.
        for phrase in ('clearing that history and running this again', 'nothing was left behind'):
            with self.subTest(absent=phrase):
                self.assertNotIn(phrase, body)

    def test_the_cleanup_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add', 'migrate')
        response = self.client.post(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.cleanup.assert_called_once()

    def test_the_cleanup_refuses_while_one_is_already_queued(self):
        self.grant('add', 'migrate')
        self.record(MigrationCleanupJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_cleanup'))
        self.assertHttpStatus(response, 302)
        self.cleanup.assert_not_called()

    def test_a_pass_left_running_names_its_job_until_the_job_is_deleted(self):
        self.grant('add', 'migrate')
        self.grant_job_view()
        passes = (
            ('migration_stage', MigrationStagingJob, self.staging, {}),
            ('migration_cutover', MigrationCutoverJob, self.cutover, {'confirm': 'true', 'backup_taken': 'on'}),
            ('migration_activate', MigrationActivationJob, self.activation, {}),
            ('migration_repoint', MigrationReferencesJob, self.references, {}),
            ('migration_cleanup', MigrationCleanupJob, self.cleanup, {}),
        )
        for name, job_class, enqueue, data in passes:
            with self.subTest(route=name):
                stale = self.record(job_class, status=JobStatusChoices.STATUS_RUNNING)
                response = self.client.post(self.url(name), data, follow=True)
                message = str(list(response.context['messages'])[0])
                self.assertIn(f'Job {stale.pk} ', message)
                self.assertIn('"Stale running Jobs"', message)
                enqueue.assert_not_called()

                # A queryset delete, because Job.delete() reaches the shared Redis.
                Job.objects.filter(pk=stale.pk).delete()
                self.client.post(self.url(name), data, follow=True)
                enqueue.assert_called_once()

    def test_a_pass_still_waiting_keeps_the_queued_warning(self):
        self.grant('add')
        self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_PENDING)
        response = self.client.post(self.url('migration_stage'), follow=True)
        self.assertEqual(
            str(list(response.context['messages'])[0]), 'A Custom Script migration staging pass is already queued.'
        )
        self.staging.assert_not_called()

    def test_a_running_job_the_user_cannot_view_keeps_the_queued_warning(self):
        self.grant('add')
        self.record(MigrationStagingJob, status=JobStatusChoices.STATUS_RUNNING)
        response = self.client.post(self.url('migration_stage'), follow=True)
        self.assertEqual(
            str(list(response.context['messages'])[0]), 'A Custom Script migration staging pass is already queued.'
        )
        self.staging.assert_not_called()

    def test_the_cleanup_route_needs_the_migrate_permission(self):
        self.assertHttpStatus(self.client.post(self.url('migration_cleanup')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_cleanup')), 403)
        self.cleanup.assert_not_called()

    def test_the_cleanup_button_is_withheld_while_a_project_serves_nothing(self):
        self.grant('add', 'migrate')
        run = self.staged_fence()

        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history', 'recreate_schedules'):
            run.record_step(step, counts={})

        self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            self.assertIn('Clean up', self.client.get(self.url('migration')).content.decode())

    def test_a_staging_user_is_not_offered_the_cleanup(self):
        self.grant('add')
        run = self.open_run(MigrationStateChoices.CUTOVER)
        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history', 'recreate_schedules'):
            run.record_step(step, counts={})

        self.assertNotIn('Clean up', self.client.get(self.url('migration')).content.decode())

    def test_the_page_names_the_latest_cleanup(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationCleanupJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    # Each case asserts the context value rather than the markup, since the buttons it styles are
    # gated on a permission as well as on state. Two rendering tests cover the markup.
    def next_action(self):
        """Return the step the page names as next."""
        return self.client.get(self.url('migration')).context['next_action']

    def record_early_passes(self):
        """Record a completed inventory and staging pass, which every later step sits behind."""
        self.record(MigrationInventoryJob)
        self.record(MigrationStagingJob)

    def test_the_inventory_is_next_before_any_pass_has_run(self):
        self.grant('add')
        self.assertEqual(self.next_action(), 'inventory')

    def test_staging_is_next_once_the_inventory_has_run(self):
        self.grant('add')
        self.record(MigrationInventoryJob)
        self.assertEqual(self.next_action(), 'stage')

    def test_a_failed_inventory_leaves_the_inventory_next(self):
        # The two passes that record no step are read off their own Job, so one that did not
        # finish is not complete.
        self.grant('add')
        self.record(MigrationInventoryJob, status=JobStatusChoices.STATUS_FAILED)
        self.assertEqual(self.next_action(), 'inventory')

    def test_nothing_is_next_while_a_blocking_finding_stands(self):
        # Staging creates nothing, so the operator's next move is off this page.
        self.grant('add')
        self.with_findings(self.finding('blocking', 'report_style', 'audit/legacy_report.py'))
        self.assertIsNone(self.next_action())

    def test_the_cutover_is_next_once_a_run_is_staged(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.open_run(MigrationStateChoices.STAGING)
        self.assertEqual(self.next_action(), 'cutover')

    def test_nothing_is_next_while_a_mapped_project_would_serve_nothing(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.open_run(MigrationStateChoices.STAGING)
        blocked = [{'project_key': 'automation', 'reason': 'holds no revision'}]

        with mock.patch.object(cutover, 'unservable_projects', return_value=blocked):
            self.assertIsNone(self.next_action())

    def test_activation_is_next_once_the_fence_is_recorded(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        run = self.open_run(MigrationStateChoices.CUTOVER)
        run.record_step(cutover.STEP, counts={})
        self.assertEqual(self.next_action(), 'activate')

    def test_repointing_is_next_once_every_project_serves(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.staged_fence()

        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            self.assertEqual(self.next_action(), 'repoint')

    def test_activation_is_offered_again_while_a_project_serves_nothing(self):
        # staged_fence leaves the Projects serving nothing.
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.staged_fence()
        self.assertEqual(self.next_action(), 'activate')

    def test_the_cleanup_is_next_once_every_reference_step_is_recorded(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        run = self.staged_fence()

        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history', 'recreate_schedules'):
            run.record_step(step, counts={})

        # Every Project serving, or activation would be named again rather than the cleanup.
        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            self.assertEqual(self.next_action(), 'cleanup')

    def test_verification_is_next_once_the_cleanup_has_finished(self):
        # No open run, which is what a completed cleanup leaves behind.
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.closed_run()
        self.assertEqual(self.next_action(), 'verify')

    def test_a_verification_run_before_the_cleanup_leaves_it_outstanding(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.record(MigrationVerificationJob)
        self.closed_run()

        self.assertEqual(self.next_action(), 'verify')

    def test_a_failed_verification_leaves_it_outstanding(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.closed_run()
        self.record(MigrationVerificationJob, status=JobStatusChoices.STATUS_FAILED)

        self.assertEqual(self.next_action(), 'verify')

    def test_nothing_is_next_once_the_verification_has_run(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.closed_run()
        self.record(MigrationVerificationJob)
        self.assertIsNone(self.next_action())

    def test_a_closed_run_is_migrated_even_when_its_cleanup_job_never_finished(self):
        self.grant('add')
        self.record_early_passes()
        self.record(MigrationCleanupJob, status=JobStatusChoices.STATUS_RUNNING)
        self.closed_run()

        self.assertEqual(self.next_action(), 'verify')

    def test_the_next_step_renders_filled_and_the_rest_as_outlines(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        self.open_run(MigrationStateChoices.STAGING)

        body = self.client.get(self.url('migration')).content.decode()

        # The cutover is next and destructive, so it fills in red rather than turning primary.
        self.assertIn('btn btn-danger', body)
        self.assertNotIn('btn btn-primary', body)
        self.assertIn('btn btn-outline-primary', body)

    def test_the_verification_renders_last_and_the_buttons_carry_their_position(self):
        self.grant('add', 'migrate')
        self.record_early_passes()
        run = self.staged_fence()

        for step in ('repoint_event_rules', 'repoint_permissions', 'repoint_job_history', 'recreate_schedules'):
            run.record_step(step, counts={})

        # Every Project serving, so all seven buttons render and the order is the whole sequence.
        with mock.patch.object(cutover, 'projects_not_serving', return_value=[]):
            body = self.client.get(self.url('migration')).content.decode()

        self.assertLess(body.index('6. Clean up'), body.index('7. Verify'))

    def test_the_verify_button_is_offered_at_every_state(self):
        # It reads only, so unlike every other pass it waits on nothing.
        self.grant('add')

        self.assertIn('Verify', self.client.get(self.url('migration')).content.decode())

    def test_the_verify_route_queues_the_job_and_returns_to_the_page(self):
        self.grant('add')
        response = self.client.post(self.url('migration_verify'))
        self.assertHttpStatus(response, 302)
        self.assertEqual(response.url, self.url('migration'))
        self.verification.assert_called_once()

    def test_the_verify_route_takes_the_staging_permission_not_the_migrate_one(self):
        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 403)

        self.grant('add')

        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 302)
        self.verification.assert_called_once()

    def test_the_verify_route_is_never_refused_for_being_queued(self):
        # It writes nothing, so two at once race on nothing.
        self.grant('add')
        self.record(MigrationVerificationJob, status=JobStatusChoices.STATUS_RUNNING)

        self.assertHttpStatus(self.client.post(self.url('migration_verify')), 302)
        self.verification.assert_called_once()

    def test_the_page_names_the_latest_verification(self):
        self.grant('add')
        self.grant_job_view()
        job = self.record(MigrationVerificationJob)
        self.assertIn(job.get_absolute_url(), self.client.get(self.url('migration')).content.decode())

    def verified(self, data, status=JobStatusChoices.STATUS_COMPLETED):
        """Record a verification Job carrying the supplied report data."""
        job = self.record(MigrationVerificationJob, status=status)
        job.data = data
        job.save()
        return job

    def test_the_verification_result_is_shown_apart_from_the_job_status(self):
        self.grant('add')
        self.grant_job_view()
        job = self.verified(None)

        for status, badge in (('ready', READY_BADGE), ('warning', WARNING_BADGE), ('blocking', BLOCKING_BADGE)):
            with self.subTest(status=status):
                job.data = {'status': status, 'checks': []}
                job.save()
                response = self.client.get(self.url('migration'))
                body = response.content.decode()

                self.assertEqual(response.context['verification_status'], status)
                self.assertInHTML(badge, body)
                self.assertInHTML('<span class="badge text-bg-green">Completed</span>', body)

    def test_a_verification_without_a_report_shows_no_result(self):
        self.grant('add')
        self.grant_job_view()
        job = self.verified(None)
        cases = (
            (JobStatusChoices.STATUS_COMPLETED, None),
            (JobStatusChoices.STATUS_COMPLETED, []),
            (JobStatusChoices.STATUS_COMPLETED, ['ready']),
            (JobStatusChoices.STATUS_COMPLETED, {'status': 'unknown'}),
            (JobStatusChoices.STATUS_RUNNING, None),
            (JobStatusChoices.STATUS_ERRORED, None),
        )

        for status, data in cases:
            with self.subTest(status=status, data=data):
                job.status = status
                job.data = data
                job.save()
                body = self.client.get(self.url('migration')).content.decode()

                self.assertIn('No result available', body)
                for badge in (READY_BADGE, WARNING_BADGE, BLOCKING_BADGE):
                    self.assertInHTML(badge, body, count=0)

    def test_an_unfinished_verification_shows_no_result_from_its_data(self):
        # A requeued Job keeps its last report until the new run ends.
        self.grant('add')
        self.grant_job_view()
        job = self.verified({'status': 'ready', 'checks': []})
        statuses = (
            JobStatusChoices.STATUS_PENDING,
            JobStatusChoices.STATUS_RUNNING,
            JobStatusChoices.STATUS_FAILED,
            JobStatusChoices.STATUS_ERRORED,
        )

        for status in statuses:
            with self.subTest(status=status):
                Job.objects.filter(pk=job.pk).update(status=status)
                response = self.client.get(self.url('migration'))

                self.assertIsNone(response.context['verification_status'])
                self.assertIn('No result available', response.content.decode())
                self.assertInHTML(READY_BADGE, response.content.decode(), count=0)

    def test_the_result_row_is_absent_before_any_verification(self):
        self.grant('add')
        response = self.client.get(self.url('migration'))

        self.assertNotIn('Verification result', response.content.decode())
        self.assertIsNone(response.context['verification_status'])

    def test_the_result_follows_the_newest_verification(self):
        self.grant('add')
        self.grant_job_view()
        older = self.verified({'status': 'ready', 'checks': []})
        Job.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(hours=1))
        self.verified(None, status=JobStatusChoices.STATUS_FAILED)

        body = self.client.get(self.url('migration')).content.decode()

        self.assertIn('No result available', body)
        self.assertInHTML(READY_BADGE, body, count=0)

    def verification_status(self):
        """Return the verification status the Migration page shows."""
        return self.client.get(self.url('migration')).context['verification_status']

    def test_a_new_run_withholds_the_result_of_an_earlier_verification(self):
        self.grant('add')
        self.grant_job_view()
        self.closed_run()
        job = self.verified({'status': 'ready', 'checks': []})
        self.assertEqual(self.verification_status(), 'ready')

        run = self.open_run(MigrationStateChoices.STAGING)
        response = self.client.get(self.url('migration'))
        self.assertIsNone(response.context['verification_status'])
        self.assertIn(job.get_absolute_url(), response.content.decode())

        run.advance(MigrationStateChoices.CUTOVER)
        run.advance(MigrationStateChoices.MIGRATED)
        self.assertIsNone(self.verification_status())

    def test_the_close_withholds_a_result_from_before_the_cleanup(self):
        self.grant('add', 'migrate')
        self.grant_job_view()
        self.record_early_passes()
        run = self.open_run(MigrationStateChoices.STAGING)
        self.verified({'status': 'ready', 'checks': []})
        self.assertEqual(self.verification_status(), 'ready')

        run.advance(MigrationStateChoices.CUTOVER)
        run.advance(MigrationStateChoices.MIGRATED)
        self.assertIsNone(self.verification_status())
        self.assertEqual(self.next_action(), 'verify')

        self.verified({'status': 'ready', 'checks': []})
        self.assertEqual(self.verification_status(), 'ready')
        self.assertIsNone(self.next_action())

    @staticmethod
    def guide_anchors(body):
        """Return the guide sections a page links to."""
        return set(re.findall(re.escape(MIGRATION_GUIDE_URL) + r'#([\w-]+)', body))

    def guide_pages(self):
        """Return the Migration page, the staging confirmation and both renders of the cutover confirmation."""
        self.grant('add', 'migrate')
        self.grant_job_view()
        # A row and a verification, so the Projects paragraph and the result row render.
        self.inventoried('guided')
        self.verified({'status': 'ready', 'checks': []})
        # Posted without the backup acknowledged, so the confirmation renders again.
        refused = self.client.post(self.url('migration_cutover'), {'confirm': 'true'})
        self.assertHttpStatus(refused, 200)
        return {
            'migration': self.client.get(self.url('migration')).content.decode(),
            'stage': self.client.get(self.url('migration_stage')).content.decode(),
            'cutover': self.client.get(self.url('migration_cutover')).content.decode(),
            'cutover refused': refused.content.decode(),
        }

    def test_each_migration_page_links_its_sections_of_the_guide(self):
        expected = {
            'migration': {'how-modules-become-projects', 'repoint-references', 'verify-the-migration', 'enter-cutover'},
            'stage': {'staging'},
            'cutover': {'repoint-references'},
            'cutover refused': {'repoint-references'},
        }
        for page, body in self.guide_pages().items():
            with self.subTest(page=page):
                self.assertEqual(self.guide_anchors(body), expected[page])

    def test_every_guide_link_names_a_heading_of_the_guide(self):
        if not GUIDE.is_file():
            self.skipTest('the guide ships with the repository, not inside the package')
        headings = re.findall(r'^#+ (.+)$', GUIDE.read_text(), flags=re.MULTILINE)
        slugs = {re.sub(r'[\s-]+', '-', re.sub(r'[^\w\s-]', '', heading).strip().lower()) for heading in headings}
        linked = set().union(*(self.guide_anchors(body) for body in self.guide_pages().values()))

        self.assertTrue(linked)
        self.assertEqual(linked - slugs, set())

    def test_no_template_syntax_reaches_the_migration_pages(self):
        # Django renders a {# #} or {{ }} that spans a line break as text.
        for page, body in self.guide_pages().items():
            for token in ('{#', '#}', '{{', '}}', '{%', '%}'):
                with self.subTest(page=page, token=token):
                    self.assertNotIn(token, body)
