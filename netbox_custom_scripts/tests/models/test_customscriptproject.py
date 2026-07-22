import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from core.models import DataSource
from netbox_custom_scripts.choices import ProjectSourceTypeChoices
from netbox_custom_scripts.models import CustomScriptProject


class CustomScriptProjectTestCase(TestCase):
    def test_create_customscriptproject(self):
        instance = CustomScriptProject.objects.create(
            name='Sample Project 1',
            key='sample-project-1',
            description='Created by the test suite.',
        )
        self.assertIsNotNone(instance.pk)
        self.assertEqual(instance.name, 'Sample Project 1')
        self.assertEqual(instance.source_type, ProjectSourceTypeChoices.UPLOAD)
        self.assertTrue(instance.enabled)

    def test_str(self):
        instance = CustomScriptProject(name='Sample Project 2', key='sample-project-2')
        self.assertEqual(str(instance), 'Sample Project 2')

    def test_absolute_url(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 3', key='sample-project-3')
        url = instance.get_absolute_url()
        self.assertEqual(
            url,
            reverse('plugins:netbox_custom_scripts:customscriptproject', args=[instance.pk]),
        )

    def test_storage_key_autoassigned(self):
        instance1 = CustomScriptProject.objects.create(name='Sample Project 4', key='sample-project-4')
        instance2 = CustomScriptProject.objects.create(name='Sample Project 5', key='sample-project-5')
        self.assertIsNotNone(instance1.storage_key)
        self.assertIsNotNone(instance2.storage_key)
        self.assertNotEqual(instance1.storage_key, instance2.storage_key)

    def test_upload_project_rejects_data_source(self):
        data_source = DataSource.objects.create(
            name='Data Source 1',
            type='local',
            source_url='file:///tmp/data-source-1/',
        )
        instance = CustomScriptProject(
            name='Sample Project 6',
            key='sample-project-6',
            source_type=ProjectSourceTypeChoices.UPLOAD,
            data_source=data_source,
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_source', cm.exception.message_dict)

    def test_upload_project_rejects_data_path(self):
        # The edit form no longer offers data_path on uploads; guard the clean() branch here.
        instance = CustomScriptProject(
            name='Sample Project 16',
            key='sample-project-16',
            source_type=ProjectSourceTypeChoices.UPLOAD,
            data_path='automation/netbox',
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_source_project_requires_data_source(self):
        instance = CustomScriptProject(
            name='Sample Project 7',
            key='sample-project-7',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_source', cm.exception.message_dict)

    def test_data_path_normalization(self):
        data_source = DataSource.objects.create(
            name='Data Source 2',
            type='local',
            source_url='file:///tmp/data-source-2/',
        )
        for raw, expected in (
            ('automation/netbox', 'automation/netbox'),
            ('automation/netbox/', 'automation/netbox'),
            ('./automation//netbox', 'automation/netbox'),
            (' automation/netbox ', 'automation/netbox'),
        ):
            with self.subTest(raw=raw):
                instance = CustomScriptProject(
                    name='Sample Project 8',
                    key='sample-project-8',
                    source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                    data_source=data_source,
                    data_path=raw,
                )
                instance.full_clean()
                self.assertEqual(instance.data_path, expected)

    def test_data_path_rejects_unsafe_values(self):
        data_source = DataSource.objects.create(
            name='Data Source 3',
            type='local',
            source_url='file:///tmp/data-source-3/',
        )
        for bad in ('/absolute/path', '../up', 'a/../b', 'a\\b', 'a\x00b'):
            with self.subTest(bad=bad):
                instance = CustomScriptProject(
                    name='Sample Project 9',
                    key='sample-project-9',
                    source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                    data_source=data_source,
                    data_path=bad,
                )
                with self.assertRaises(ValidationError) as cm:
                    instance.full_clean()
                self.assertIn('data_path', cm.exception.message_dict)

    def test_data_source_project_requires_nonempty_data_path(self):
        data_source = DataSource.objects.create(
            name='Data Source 4',
            type='local',
            source_url='file:///tmp/data-source-4/',
        )
        instance = CustomScriptProject(
            name='Sample Project 10',
            key='sample-project-10',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='',
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_key_and_source_type_immutable(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 11', key='sample-project-11')
        instance.key = 'sample-project-11-renamed'
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('key', cm.exception.message_dict)

        instance.refresh_from_db()
        instance.source_type = ProjectSourceTypeChoices.DATA_SOURCE
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('source_type', cm.exception.message_dict)

    def test_storage_key_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 12', key='sample-project-12')
        instance.storage_key = uuid.uuid4()
        with self.assertRaises(ValidationError):
            instance.save()

    def test_key_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 14', key='sample-project-14')
        instance.key = 'sample-project-14-renamed'
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('key', cm.exception.message_dict)

    def test_source_type_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 15', key='sample-project-15')
        instance.source_type = ProjectSourceTypeChoices.DATA_SOURCE
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_type', cm.exception.message_dict)

    def test_db_constraint_blocks_orm_bypass(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 13', key='sample-project-13')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProject.objects.filter(pk=instance.pk).update(data_path='sneaky/path')
