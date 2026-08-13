import hashlib
import tempfile
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import TestCase, override_settings
from django.utils import timezone

from core.choices import JobStatusChoices, ManagedFileRootPathChoices
from core.models import DataFile, DataSource
from extras.models import Script, ScriptModule
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.jobs import MigrationStagingJob, RevisionValidationJob
from netbox_custom_scripts.migration import plan, source, staging
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.tests.runtime.test_cache import discard_tree
from netbox_custom_scripts.tests.storage.test_service import IN_MEMORY_STORAGES

LEGACY_SCRIPT = b"""from extras.scripts import Script


class Deploy(Script):
    def run(self, data, commit):
        return 'deployed'
"""

NATIVE_SCRIPT = b"""from netbox_custom_scripts.scripts import Script


class Provision(Script):
    def run(self, data, commit):
        return 'provisioned'
"""

# Beside the script in the repository and never a legacy module of its own, because the built-in
# feature syncs one file per row. A migrated project holds it.
HELPER = b"""def describe():
    return 'helper'
"""


class StageTestCase(TestCase):
    """Staging creates Projects and revisions and activates nothing."""

    def setUp(self):
        scripts_root = tempfile.mkdtemp(prefix='legacy-scripts-')
        self.addCleanup(discard_tree, Path(scripts_root))
        self.enterContext(
            override_settings(
                STORAGES={
                    **IN_MEMORY_STORAGES,
                    'scripts': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': scripts_root, 'allow_overwrite': True},
                    },
                }
            )
        )
        self.cache_root = Path(tempfile.mkdtemp())
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)

        self.data_source = DataSource.objects.create(
            name='Automation', type='local', source_url='file:///tmp/automation'
        )
        self.synced = self.legacy_synced_module('automation/deploy.py', LEGACY_SCRIPT)
        self.data_file('automation/helpers.py', HELPER)
        self.uploaded = self.legacy_uploaded_module('provision.py', NATIVE_SCRIPT)

    def data_file(self, path, content):
        """Add one file to the Data Source's synchronized inventory."""
        # last_updated is editable=False with no auto_now, so a fixture has to set it.
        return DataFile.objects.create(
            source=self.data_source,
            path=path,
            size=len(content),
            hash=hashlib.sha256(content).hexdigest(),
            data=content,
            last_updated=timezone.now(),
        )

    def legacy_synced_module(self, path, content):
        """Create a built-in script module fed by a Data Source file."""
        module = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=self.data_file(path, content))
        # file_root is validated before save() forces it, and full_clean() writes the bytes.
        module.full_clean()
        module.save()
        return module

    def legacy_uploaded_module(self, path, content):
        """Create a built-in script module holding uploaded content."""
        storages['scripts'].save(path, ContentFile(content))
        return ScriptModule.objects.create(file_path=path)

    def stage_all(self):
        modules = source.legacy_modules()
        return staging.stage(plan.group(modules), modules)

    def project_for(self, source_type):
        return CustomScriptProject.objects.get(source_type=source_type)

    def test_a_synced_directory_becomes_a_project_holding_its_whole_folder(self):
        self.stage_all()
        project = self.project_for(ProjectSourceTypeChoices.DATA_SOURCE)
        self.assertEqual(project.data_path, 'automation')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.MANUAL)
        revision = CustomScriptProjectRevision.objects.get(project=project)
        self.assertEqual(sorted(entry['path'] for entry in revision.manifest), ['deploy.py', 'helpers.py'])
        declared = CustomScriptModule.objects.get(project=project)
        self.assertEqual(declared.source_path, 'deploy.py')
        self.assertTrue(declared.enabled)

    def test_an_uploaded_module_becomes_its_own_project(self):
        self.stage_all()
        project = self.project_for(ProjectSourceTypeChoices.UPLOAD)
        self.assertEqual(project.data_path, '')
        self.assertEqual(project.activation_policy, ActivationPolicyChoices.MANUAL)
        revision = CustomScriptProjectRevision.objects.get(project=project)
        self.assertEqual([entry['path'] for entry in revision.manifest], ['provision.py'])
        self.assertEqual(CustomScriptModule.objects.get(project=project).source_path, 'provision.py')

    def test_running_staging_twice_changes_nothing(self):
        # Deterministic identity plus content addressing, so the second pass creates no rows.
        self.stage_all()
        projects = CustomScriptProject.objects.count()
        revision_pks = set(CustomScriptProjectRevision.objects.values_list('pk', flat=True))
        declarations = CustomScriptModule.objects.count()

        results = self.stage_all()

        self.assertEqual(CustomScriptProject.objects.count(), projects)
        self.assertEqual(set(CustomScriptProjectRevision.objects.values_list('pk', flat=True)), revision_pks)
        self.assertEqual(CustomScriptModule.objects.count(), declarations)
        self.assertTrue(all(result['created'] is False for result in results))

    def test_no_staged_revision_activates(self):
        # A verdict is reached and the project still serves nothing, because the cutover owns that.
        results = self.stage_all()
        self.assertEqual(len(results), 2)
        for result in results:
            with self.subTest(project=result['key']):
                revision = CustomScriptProjectRevision.objects.get(pk=result['revision_pk'])
                RevisionValidationJob.enqueue_validation(revision, immediate=True)
                revision.refresh_from_db()
                self.assertEqual(revision.status, RevisionStatusChoices.VALID)
                self.assertIsNone(CustomScriptProject.objects.get(pk=revision.project_id).active_revision_id)

    def test_the_job_stages_every_project_and_records_the_result(self):
        job = MigrationStagingJob.enqueue(immediate=True)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        staged = job.data['projects']
        self.assertEqual(len(staged), 2)
        self.assertTrue(all(result['created'] for result in staged))
        self.assertEqual(CustomScriptProject.objects.count(), 2)

    def test_the_job_reports_that_a_verdict_is_still_to_come(self):
        # The status staging leaves behind is never the verdict, so it must not read like one.
        job = MigrationStagingJob.enqueue(immediate=True)
        messages = ' '.join(entry['message'] for entry in job.log_entries)
        self.assertIn('queued for validation', messages)
        self.assertIn('2 awaiting a verdict', messages)
        self.assertNotIn('is materialized', messages)

    def test_the_job_reports_a_revision_that_needs_no_validation(self):
        # A second pass resolves to the revision already holding that content, verdict included.
        for result in self.stage_all():
            revision = CustomScriptProjectRevision.objects.get(pk=result['revision_pk'])
            RevisionValidationJob.enqueue_validation(revision, immediate=True)

        job = MigrationStagingJob.enqueue(immediate=True)

        messages = ' '.join(entry['message'] for entry in job.log_entries)
        self.assertIn('is Valid, so no validation was queued', messages)
        self.assertIn('0 awaiting a verdict', messages)

    def test_the_job_refuses_to_stage_when_a_finding_blocks(self):
        # A hyphenated name is not a Python identifier, so nothing could ever import it.
        self.legacy_uploaded_module('my-report.py', NATIVE_SCRIPT)

        job = MigrationStagingJob.enqueue(immediate=True)

        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        self.assertFalse(CustomScriptProject.objects.exists())
        self.assertIn('my-report.py', ' '.join(entry['message'] for entry in job.log_entries))

    def test_the_built_in_rows_are_untouched(self):
        fields = ('pk', 'file_root', 'file_path', 'data_path', 'data_source_id')
        before_modules = list(ScriptModule.objects.values(*fields).order_by('pk'))
        before_scripts = list(Script.objects.values('pk', 'module_id', 'name', 'is_executable').order_by('pk'))

        self.stage_all()

        self.assertEqual(list(ScriptModule.objects.values(*fields).order_by('pk')), before_modules)
        self.assertEqual(
            list(Script.objects.values('pk', 'module_id', 'name', 'is_executable').order_by('pk')), before_scripts
        )
