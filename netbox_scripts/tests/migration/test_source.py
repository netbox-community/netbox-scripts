import hashlib
import shutil
import tempfile
import uuid

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.db.models import Q
from django.test import TestCase, override_settings
from django.utils import timezone

from core.choices import JobStatusChoices, ManagedFileRootPathChoices
from core.events import OBJECT_UPDATED
from core.models import DataFile, DataSource, Job, ObjectType
from dcim.models import Site
from extras.models import EventRule, Script, ScriptModule
from netbox_scripts.migration import source

LEGACY_SCRIPT = b"""from extras.scripts import Script


class Deploy(Script):
    def run(self, data, commit):
        pass
"""

REPORT = b"""class Audit:
    def test_every_device_has_a_platform(self):
        pass
"""


class LegacyModulesTestCase(TestCase):
    """Reading the built-in implementation, without importing a single stored module."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The default scripts backend is rooted inside the NetBox installation. A directory rather
        # than an in-memory store, because ManagedFile writes through its own storage instance
        # while the loader reads the cached one, and only a directory is shared between the two.
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
                    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
                }
            )
        )

    @classmethod
    def setUpTestData(cls):
        cls.data_source = DataSource.objects.create(
            name='Automation', type='local', source_url='file:///tmp/automation'
        )
        # last_updated is editable=False with no auto_now, so a fixture has to set it.
        cls.data_file = DataFile.objects.create(
            source=cls.data_source,
            path='automation/netbox/deploy.py',
            size=len(LEGACY_SCRIPT),
            hash=hashlib.sha256(LEGACY_SCRIPT).hexdigest(),
            data=LEGACY_SCRIPT,
            last_updated=timezone.now(),
        )

    def synced_module(self):
        """Create a module fed by the fixture's Data Source file, as a synchronization would."""
        # file_root is validated before save() forces it, so it is set here.
        module = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=self.data_file)
        # full_clean() is what records data_path and writes the bytes into storage.
        module.full_clean()
        module.save()
        return module

    def test_an_uploaded_module_is_reported_with_no_data_source(self):
        storages['scripts'].save('provision.py', ContentFile(LEGACY_SCRIPT))
        module = ScriptModule.objects.create(file_path='provision.py')
        reported = {entry.pk: entry for entry in source.legacy_modules()}
        self.assertIn(module.pk, reported)
        self.assertIsNone(reported[module.pk].data_source_id)
        self.assertEqual(reported[module.pk].data_path, '')

    def test_a_synced_module_reports_the_repository_path_it_came_from(self):
        # The stored file is flat, so data_path is the only record of the original directory.
        module = self.synced_module()
        entry = next(item for item in source.legacy_modules() if item.pk == module.pk)
        self.assertEqual(entry.data_path, 'automation/netbox/deploy.py')
        self.assertEqual(entry.file_path, 'deploy.py')
        self.assertEqual(entry.data_source_id, self.data_source.pk)

    def test_a_module_reports_the_scripts_it_published_with_their_keys(self):
        module = self.synced_module()
        entry = next(item for item in source.legacy_modules() if item.pk == module.pk)
        self.assertEqual(entry.python_name, 'deploy')
        self.assertEqual([script.name for script in entry.scripts], ['Deploy'])
        # The key is what every reference a migration repoints actually holds.
        self.assertEqual([script.pk for script in entry.scripts], list(module.scripts.values_list('pk', flat=True)))

    def test_read_source_returns_the_stored_bytes(self):
        module = self.synced_module()
        entry = next(item for item in source.legacy_modules() if item.pk == module.pk)
        self.assertEqual(source.read_source(entry), LEGACY_SCRIPT)

    def test_a_report_module_is_not_returned_at_all(self):
        # The manager admits reports, whose bytes are under REPORTS_ROOT, so one can never be read.
        storages['scripts'].save('audit.py', ContentFile(REPORT))
        module = ScriptModule.objects.create(file_path='audit.py')
        # save() forces the root to scripts, so a legacy report's root is set past it.
        ScriptModule.objects.filter(pk=module.pk).update(file_root=ManagedFileRootPathChoices.REPORTS)

        self.assertNotIn(module.pk, [item.pk for item in source.legacy_modules()])
        self.assertEqual(source.legacy_report_count(), 1)
        self.assertNotIn(module.pk, source.legacy_script_module_keys())

    def report_module(self):
        """Create a REPORTS-root module, which this migration does not cover."""
        storages['scripts'].save('audit.py', ContentFile(REPORT))
        module = ScriptModule.objects.create(file_path='audit.py')
        # save() forces the root to scripts, so a legacy report's root is set past it.
        ScriptModule.objects.filter(pk=module.pk).update(file_root=ManagedFileRootPathChoices.REPORTS)
        return module

    @staticmethod
    def legacy_job(object_type, object_id):
        return Job.objects.create(
            name='run',
            object_type=object_type,
            object_id=object_id,
            job_id=uuid.uuid4(),
            status=JobStatusChoices.STATUS_PENDING,
            queue_name='default',
        )

    @staticmethod
    def keys(queryset):
        return set(queryset.values_list('pk', flat=True))

    @staticmethod
    def module_type():
        return ObjectType.objects.get_for_model(ScriptModule, for_concrete_model=False)

    @staticmethod
    def class_type():
        return ObjectType.objects.get_for_model(Script, for_concrete_model=False)

    def test_the_job_readers_exclude_a_report_module(self):
        job = self.legacy_job(self.module_type(), self.report_module().pk)

        self.assertNotIn(job.pk, self.keys(source.script_jobs()))
        self.assertNotIn(job.pk, self.keys(source.enqueued_script_jobs()))

    def test_the_job_readers_exclude_a_report_class(self):
        audit = Script.objects.create(module=self.report_module(), name='Audit')
        job = self.legacy_job(self.class_type(), audit.pk)

        self.assertNotIn(job.pk, self.keys(source.script_jobs()))

    def test_the_job_readers_still_carry_a_script_module(self):
        """The narrowing must not drop what the readers exist to find."""
        # A report module makes the clause live, and one sequence means the keys cannot collide.
        self.report_module()
        job = self.legacy_job(self.module_type(), self.synced_module().pk)

        self.assertIn(job.pk, self.keys(source.script_jobs()))

    def test_the_job_readers_still_carry_a_script_class(self):
        Script.objects.create(module=self.report_module(), name='Audit')
        deploy = self.synced_module().scripts.get(name='Deploy')
        job = self.legacy_job(self.class_type(), deploy.pk)

        self.assertIn(job.pk, self.keys(source.script_jobs()))

    def test_the_predicate_pairs_each_object_type_with_its_own_keys(self):
        """
        A union would let a report class key exclude a module Job that happens to share it.

        Also why the scope is subtracted rather than selected: a reference whose type and id
        disagree survives an upgrade, and selecting the scripts root would hide it.
        """
        report = self.report_module()
        audit = Script.objects.create(module=report, name='Audit')

        predicate = source._report_predicate('object_type', 'object_id')

        # Two Q children, not one clause of two conditions: a union flattens to the latter.
        self.assertEqual(len(predicate.children), 2)
        self.assertTrue(all(isinstance(child, Q) for child in predicate.children))
        clauses = [dict(child.children) for child in predicate.children]
        self.assertEqual(clauses[0]['object_id__in'], [report.pk])
        self.assertEqual(clauses[1]['object_id__in'], [audit.pk])

    def test_an_event_rule_on_a_report_class_is_excluded(self):
        audit = Script.objects.create(module=self.report_module(), name='Audit')
        rule = EventRule.objects.create(
            name='on report',
            event_types=[OBJECT_UPDATED],
            action_type='script',
            action_object_type=self.class_type(),
            action_object_id=audit.pk,
        )
        rule.object_types.add(ObjectType.objects.get_for_model(Site))

        self.assertNotIn(rule.pk, self.keys(source.legacy_event_rules()))

    def test_an_event_rule_on_a_script_class_is_carried(self):
        Script.objects.create(module=self.report_module(), name='Audit')
        deploy = self.synced_module().scripts.get(name='Deploy')
        rule = EventRule.objects.create(
            name='on script',
            event_types=[OBJECT_UPDATED],
            action_type='script',
            action_object_type=self.class_type(),
            action_object_id=deploy.pk,
        )
        rule.object_types.add(ObjectType.objects.get_for_model(Site))

        self.assertIn(rule.pk, self.keys(source.legacy_event_rules()))

    def test_reference_counts_are_zero_on_a_clean_installation(self):
        counts = source.reference_counts()
        self.assertEqual(counts['event_rules'], 0)
        self.assertEqual(counts['permissions'], 0)
        self.assertEqual(counts['jobs'], 0)
