from django.test import TestCase

from netbox_custom_scripts.forms import (
    CustomScriptModuleBulkEditForm,
    CustomScriptModuleBulkImportForm,
    CustomScriptModuleEditForm,
    CustomScriptModuleFilterForm,
)
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject


class CustomScriptModuleEditFormTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Form Project 1', key='form-project-1')
        cls.other_project = CustomScriptProject.objects.create(name='Form Project 2', key='form-project-2')

    def test_good_path(self):
        form = CustomScriptModuleEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'tools/deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_project_is_required(self):
        form = CustomScriptModuleEditForm(data={'source_path': 'deploy.py'})
        self.assertFalse(form.is_valid())
        self.assertIn('project', form.errors)

    def test_source_path_is_required(self):
        form = CustomScriptModuleEditForm(data={'project': self.project.pk})
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_canonicalizes_the_source_path(self):
        form = CustomScriptModuleEditForm(
            data={
                'project': self.project.pk,
                'source_path': './tools//deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.source_path, 'tools/deploy.py')

    def test_rejects_an_unimportable_path(self):
        form = CustomScriptModuleEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'tools/deploy.txt',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_rejects_a_traversal_path(self):
        form = CustomScriptModuleEditForm(
            data={
                'project': self.project.pk,
                'source_path': '../outside.py',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_rejects_a_case_folded_sibling(self):
        CustomScriptModule.objects.create(project=self.project, source_path='tools/deploy.py')
        form = CustomScriptModuleEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'Tools/Deploy.py',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_accepts_the_same_path_on_another_project(self):
        CustomScriptModule.objects.create(project=self.project, source_path='tools/deploy.py')
        form = CustomScriptModuleEditForm(
            data={
                'project': self.other_project.pk,
                'source_path': 'tools/deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_discovery_fields_are_not_form_fields(self):
        form = CustomScriptModuleEditForm()
        for field in ('discovery_status', 'discovery_error', 'last_discovered_revision'):
            with self.subTest(field=field):
                self.assertNotIn(field, form.fields)

    def test_tags_included_in_fieldsets(self):
        field_names = [item for fieldset in CustomScriptModuleEditForm.fieldsets for item in fieldset.items]
        self.assertIn('tags', field_names)


class CustomScriptModuleBulkEditFormTestCase(TestCase):
    def test_source_path_is_not_bulk_editable(self):
        self.assertNotIn('source_path', CustomScriptModuleBulkEditForm().fields)

    def test_discovery_fields_are_not_bulk_editable(self):
        form = CustomScriptModuleBulkEditForm()
        for field in ('discovery_status', 'discovery_error', 'last_discovered_revision'):
            with self.subTest(field=field):
                self.assertNotIn(field, form.fields)


class CustomScriptModuleFilterFormTestCase(TestCase):
    def test_empty_filter_is_valid(self):
        form = CustomScriptModuleFilterForm(data={})
        self.assertTrue(form.is_valid(), form.errors)


class CustomScriptModuleBulkImportFormTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Import Project', key='import-project')

    def test_good_path(self):
        form = CustomScriptModuleBulkImportForm(
            data={
                'project': self.project.key,
                'source_path': 'tools/deploy.py',
                'enabled': True,
                'description': 'CSV-imported',
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_missing_required_field(self):
        form = CustomScriptModuleBulkImportForm(data={'description': 'No project or path'})
        self.assertFalse(form.is_valid())
        self.assertIn('project', form.errors)
        self.assertIn('source_path', form.errors)

    def test_unknown_project_key_is_rejected(self):
        form = CustomScriptModuleBulkImportForm(
            data={
                'project': 'no-such-project',
                'source_path': 'deploy.py',
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('project', form.errors)
