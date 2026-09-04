import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings

from core.choices import JobStatusChoices
from netbox_scripts import branching
from netbox_scripts.choices import ActivationPolicyChoices, RevisionStatusChoices
from netbox_scripts.forms import ScriptProjectEntrypointsForm
from netbox_scripts.ingestion import current_source_tree, ingest_upload
from netbox_scripts.jobs import ProjectEntrypointRefreshJob, RevisionValidationJob
from netbox_scripts.models import (
    CustomScript,
    CustomScriptModule,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.storage import service, store
from netbox_scripts.storage.exceptions import StorageError

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}

ALPHA = b"""from netbox_scripts.scripts import Script


class Alpha(Script):
    class Meta:
        name = 'Alpha'

    def run(self, data, commit):
        return 'alpha'
"""

BETA = b"""from netbox_scripts.scripts import Script


class Beta(Script):
    class Meta:
        name = 'Beta'

    def run(self, data, commit):
        return 'beta'
"""


def two_entrypoint_project(key='deploy-devices', **kwargs):
    """Return a project holding two uploaded files, both declared and enabled."""
    project = ScriptProject.objects.create(name=key.replace('-', ' ').title(), key=key, **kwargs)
    ingest_upload(project, filename='alpha.py', content=ALPHA)
    # Re-read rather than refresh, because current_revision is cached per instance and the
    # second upload has to stage the tree the first one left behind.
    project = ScriptProject.objects.get(pk=project.pk)
    ingest_upload(project, filename='beta.py', content=BETA, base_files=current_source_tree(project))
    return ScriptProject.objects.get(pk=project.pk)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class EntrypointRefreshEnqueueTestCase(TestCase):
    """What the tab leaves behind for a worker to pick up."""

    def setUp(self):
        self.enterContext(mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None))
        self.project = two_entrypoint_project()

    def test_the_job_row_carries_the_project(self):
        # The pk travels in the payload, because Job.clean() refuses an instance link for a model
        # without the jobs feature.
        job = ProjectEntrypointRefreshJob.enqueue_refresh(self.project)
        job.refresh_from_db()
        self.assertEqual(job.data, {'project_id': self.project.pk})

    def test_enqueueing_does_no_storage_work_of_its_own(self):
        with mock.patch.object(service, 'refresh_revision_entrypoints') as refresh:
            ProjectEntrypointRefreshJob.enqueue_refresh(self.project)
        refresh.assert_not_called()


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class ProjectEntrypointRefreshJobTestCase(TestCase):
    """The Job body: it restages stored content under the selection as it stands now."""

    def setUp(self):
        self.validated = self.enterContext(
            mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None)
        )
        self.project = two_entrypoint_project()
        # An operator edits entrypoints on a project whose source already reached a verdict, so
        # the fixture gives it one. A revision still awaiting one is a separate case below.
        ScriptProjectRevision.objects.filter(pk=self.project.current_revision.pk).update(
            status=RevisionStatusChoices.VALID
        )
        self.project = ScriptProject.objects.get(pk=self.project.pk)
        self.validated.reset_mock()

    def run_job(self, project_id=None):
        """Run one refresh Job inline and return it, with the project re-read."""
        job = ProjectEntrypointRefreshJob.enqueue(
            immediate=True, project_id=self.project.pk if project_id is None else project_id
        )
        job.refresh_from_db()
        self.project = ScriptProject.objects.get(pk=self.project.pk)
        return job

    def test_a_changed_selection_stages_a_new_revision(self):
        before = self.project.revisions.count()
        self.project.select_entrypoints(['alpha.py'])

        self.assertEqual(self.run_job().status, JobStatusChoices.STATUS_COMPLETED)

        self.assertEqual(self.project.revisions.count(), before + 1)
        revision = self.project.latest_revision()
        self.assertEqual([entry['source_path'] for entry in revision.entrypoint_snapshot], ['alpha.py'])

    def test_the_new_revision_holds_the_content_unchanged(self):
        # The whole point of the primitive: the same stored tree under a new configuration, so
        # the source digest is untouched and only the entrypoint digest moves.
        source = self.project.latest_revision()
        self.project.select_entrypoints(['alpha.py'])
        self.run_job()

        refreshed = self.project.latest_revision()
        self.assertEqual(refreshed.digest, source.digest)
        self.assertNotEqual(refreshed.entrypoint_digest, source.entrypoint_digest)
        self.assertEqual([entry['path'] for entry in refreshed.manifest], ['alpha.py', 'beta.py'])

    def test_the_refresh_restages_the_newest_stored_tree_rather_than_the_active_one(self):
        # The selection can change while a newer revision is still waiting for activation.
        # Restaging what the project serves would drop that revision's content silently.
        active = self.project.revisions.order_by('created').first()
        ScriptProjectRevision.objects.filter(pk=active.pk).update(status=RevisionStatusChoices.ACTIVE)
        ScriptProject.objects.filter(pk=self.project.pk).update(active_revision=active)
        self.project = ScriptProject.objects.get(pk=self.project.pk)
        self.assertEqual([entry['path'] for entry in active.manifest], ['alpha.py'])

        before = self.project.revisions.count()
        self.project.select_entrypoints(['beta.py'])
        self.run_job()

        self.assertEqual(self.project.revisions.count(), before + 1)
        refreshed = self.project.latest_revision()
        self.assertEqual([entry['path'] for entry in refreshed.manifest], ['alpha.py', 'beta.py'])

    def test_a_changed_selection_hands_the_new_revision_to_validation(self):
        self.project.select_entrypoints(['alpha.py'])
        self.run_job()
        self.validated.assert_called_once_with(self.project.latest_revision())

    def test_an_unchanged_selection_stages_nothing(self):
        before = self.project.revisions.count()
        self.assertEqual(self.run_job().status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(self.project.revisions.count(), before)

    def test_an_unchanged_selection_enqueues_no_validation(self):
        # Identical content under an identical configuration resolves to the row that already
        # holds a verdict, and only a materialized revision is claimable.
        self.run_job()
        self.validated.assert_not_called()

    def test_an_unchanged_selection_retries_a_revision_still_awaiting_a_verdict(self):
        # The other half of the claimable rule, and the same shape ingestion has: a revision
        # that never reached a verdict is still claimable, so the refresh re-drives it rather
        # than leaving it stranded.
        ScriptProjectRevision.objects.filter(pk=self.project.current_revision.pk).update(
            status=RevisionStatusChoices.MATERIALIZED
        )
        self.run_job()
        self.validated.assert_called_once()

    def test_a_project_with_no_stored_content_completes_with_nothing_to_do(self):
        empty = ScriptProject.objects.create(name='Empty', key='empty')
        job = ProjectEntrypointRefreshJob.enqueue(immediate=True, project_id=empty.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        self.assertFalse(empty.revisions.exists())

    def test_a_project_deleted_before_the_job_runs_completes_with_nothing_to_do(self):
        pk = self.project.pk
        self.project.delete()
        job = ProjectEntrypointRefreshJob.enqueue(immediate=True, project_id=pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)

    def test_unsafe_routing_fails_the_job_before_any_work(self):
        with (
            mock.patch.object(branching, 'unsafe_routing_reason', return_value='a branch is active'),
            mock.patch.object(service, 'refresh_revision_entrypoints') as refresh,
        ):
            job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        refresh.assert_not_called()

    def test_a_storage_failure_fails_the_job(self):
        self.project.select_entrypoints(['alpha.py'])
        with mock.patch.object(store, 'verify_revision_tree', side_effect=StorageError('the backend refused')):
            job = self.run_job()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.validated.assert_not_called()


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class EntrypointsFormTestCase(TestCase):
    """The tab enqueues a refresh only when the selection actually moved."""

    def setUp(self):
        self.enterContext(mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None))
        self.project = two_entrypoint_project()
        self.enqueued = self.enterContext(
            mock.patch.object(ProjectEntrypointRefreshJob, 'enqueue_refresh', return_value=None)
        )

    def save(self, paths):
        form = ScriptProjectEntrypointsForm(data={'entrypoints': paths}, instance=self.project)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        return form

    def test_a_changed_selection_enqueues_the_job(self):
        self.save(['alpha.py'])
        self.enqueued.assert_called_once_with(self.project)

    def test_an_unchanged_selection_enqueues_nothing(self):
        # A selection that did not move resolves to the revision that already exists, so the
        # comparison keeps an unchanged save out of the Job list entirely.
        self.save(['alpha.py', 'beta.py'])
        self.enqueued.assert_not_called()

    def test_an_emptied_selection_enqueues_the_job(self):
        self.save([])
        self.enqueued.assert_called_once_with(self.project)

    def test_the_declarations_are_still_reconciled(self):
        self.save(['alpha.py'])
        enabled = CustomScriptModule.objects.filter(project=self.project, enabled=True)
        self.assertEqual(set(enabled.values_list('source_path', flat=True)), {'alpha.py'})
        # Deselection is `enabled`, never a row delete, because Custom Script rows and Job
        # history reference the declaration.
        self.assertEqual(CustomScriptModule.objects.filter(project=self.project).count(), 2)


@override_settings(STORAGES=IN_MEMORY_STORAGES)
class EntrypointRefreshPolicyTestCase(TestCase):
    """The chain a selection change sets off, and what the activation policy does with it."""

    def setUp(self):
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-entrypoints-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))
        # A worker would pick the validation up. Its queue handoff never fires under TestCase, so
        # it runs inline here and the whole chain stays inside one test.
        self.enterContext(
            mock.patch.object(
                RevisionValidationJob,
                'enqueue_validation',
                side_effect=lambda revision, **kwargs: RevisionValidationJob.enqueue(
                    immediate=True, revision_pk=revision.pk
                ),
            )
        )

    def refresh(self, project):
        """Run one refresh Job to completion and return the project's newest revision."""
        job = ProjectEntrypointRefreshJob.enqueue(immediate=True, project_id=project.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        return ScriptProject.objects.get(pk=project.pk)

    def test_an_automatic_project_ends_up_serving_the_new_selection(self):
        project = two_entrypoint_project(key='automatic', activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        self.assertEqual(CustomScript.objects.filter(project=project).count(), 2)

        project.select_entrypoints(['alpha.py'])
        project = self.refresh(project)

        revision = project.latest_revision()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(project.active_revision_id, revision.pk)
        # Retirement rather than deletion, so the Job history a row accumulated survives.
        self.assertTrue(CustomScript.objects.get(project=project, class_name='Alpha').is_executable)
        self.assertTrue(CustomScript.objects.get(project=project, class_name='Beta').is_retired)

    def test_a_manual_project_reaches_valid_and_keeps_serving_what_it_had(self):
        project = two_entrypoint_project(key='manual', activation_policy=ActivationPolicyChoices.MANUAL)
        served = project.active_revision_id

        project.select_entrypoints(['alpha.py'])
        project = self.refresh(project)

        revision = project.latest_revision()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertEqual(project.active_revision_id, served)

    def test_an_upload_project_has_a_route_at_all(self):
        # The bug this fixes: an uploaded project could not apply a selection by any means,
        # because re-uploading identical content resolves to the revision that already exists.
        project = two_entrypoint_project(key='uploaded', activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        project.select_entrypoints([])
        project = self.refresh(project)

        self.assertEqual(project.latest_revision().entrypoint_snapshot, [])
        self.assertFalse(CustomScript.objects.filter(project=project, is_retired=False).exists())
