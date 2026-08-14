import hashlib
import shutil
import tempfile

from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import TestCase, override_settings
from django.utils import timezone

from core.choices import ManagedFileRootPathChoices
from core.models import DataFile, DataSource
from extras.models import ScriptModule
from netbox_custom_scripts.migration import source

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
                    'netbox_custom_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
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

    def test_a_report_module_is_reported_by_its_file_root(self):
        storages['scripts'].save('audit.py', ContentFile(REPORT))
        module = ScriptModule.objects.create(file_path='audit.py')
        # save() forces the root to scripts, so a legacy report's root is set past it.
        ScriptModule.objects.filter(pk=module.pk).update(file_root=ManagedFileRootPathChoices.REPORTS)
        entry = next(item for item in source.legacy_modules() if item.pk == module.pk)
        self.assertEqual(entry.file_root, 'reports')

    def test_reference_counts_are_zero_on_a_clean_installation(self):
        counts = source.reference_counts()
        self.assertEqual(counts['event_rules'], 0)
        self.assertEqual(counts['permissions'], 0)
        self.assertEqual(counts['jobs'], 0)
