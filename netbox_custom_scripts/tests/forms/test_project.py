from django import forms
from django.test import TestCase

from core.models import DataSource
from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_custom_scripts.forms import (
    CustomScriptProjectBulkImportForm,
    CustomScriptProjectEditForm,
    CustomScriptProjectFilterForm,
)
from netbox_custom_scripts.models import CustomScriptProject
from utilities.forms.fields import SlugField
from utilities.forms.widgets import HTMXSelect


class CustomScriptProjectEditFormTestCase(TestCase):
    def test_good_path(self):
        form = CustomScriptProjectEditForm(
            data={
                'name': 'CustomScriptProject 1',
                'key': 'project-1',
                'description': 'A valid project',
                'source_type': ProjectSourceTypeChoices.UPLOAD,
                'activation_policy': ActivationPolicyChoices.MANUAL,
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_name_is_required(self):
        form = CustomScriptProjectEditForm(data={'key': 'missing-name'})
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)

    def test_key_uniqueness(self):
        CustomScriptProject.objects.create(name='CustomScriptProject 1', key='project-1')
        form = CustomScriptProjectEditForm(
            data={
                'name': 'CustomScriptProject 2',
                'key': 'project-1',
                'source_type': ProjectSourceTypeChoices.UPLOAD,
                'activation_policy': ActivationPolicyChoices.MANUAL,
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('key', form.errors)

    def test_upload_project_omits_source_ownership_fields(self):
        # data_path is not a form field for upload projects: a posted value is simply
        # discarded. Rejection stays model-side (test_upload_project_rejects_data_path).
        form = CustomScriptProjectEditForm(
            data={
                'name': 'CustomScriptProject 3',
                'key': 'project-3',
                'source_type': ProjectSourceTypeChoices.UPLOAD,
                'data_path': 'automation/netbox/',
                'activation_policy': ActivationPolicyChoices.MANUAL,
                'enabled': True,
            }
        )
        self.assertNotIn('data_source', form.fields)
        self.assertNotIn('data_path', form.fields)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.data_path, '')

    def test_data_source_project_rejects_traversal_data_path(self):
        data_source = DataSource.objects.create(
            name='Data Source Form 1',
            type='local',
            source_url='file:///tmp/data-source-form-1/',
        )
        form = CustomScriptProjectEditForm(
            data={
                'name': 'CustomScriptProject 4',
                'key': 'project-4',
                'source_type': ProjectSourceTypeChoices.DATA_SOURCE,
                'data_source': data_source.pk,
                'data_path': '../outside',
                'activation_policy': ActivationPolicyChoices.MANUAL,
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('data_path', form.errors)

    def test_identity_fields_disabled_on_edit(self):
        project = CustomScriptProject.objects.create(name='CustomScriptProject 5', key='project-5')
        edit_form = CustomScriptProjectEditForm(instance=project)
        self.assertTrue(edit_form.fields['key'].disabled)
        self.assertTrue(edit_form.fields['source_type'].disabled)
        # The slug widget (and its regenerate button) applies only while key is editable.
        self.assertIs(type(edit_form.fields['key'].widget), forms.TextInput)

        create_form = CustomScriptProjectEditForm()
        self.assertFalse(create_form.fields['key'].disabled)
        self.assertFalse(create_form.fields['source_type'].disabled)

    def test_key_uses_slug_ux_on_create(self):
        form = CustomScriptProjectEditForm()
        self.assertIsInstance(form.fields['key'], SlugField)
        self.assertEqual(form.fields['key'].widget.attrs.get('slug-source'), 'name')

    def test_tags_included_in_fieldsets(self):
        field_names = [item for fieldset in CustomScriptProjectEditForm.fieldsets for item in fieldset.items]
        self.assertIn('tags', field_names)

    def test_new_project_form_defaults_to_upload_without_source_ownership_fields(self):
        form = CustomScriptProjectEditForm()
        self.assertNotIn('data_source', form.fields)
        self.assertNotIn('data_path', form.fields)

    def test_data_source_selection_exposes_source_ownership_fields(self):
        form = CustomScriptProjectEditForm(data={'source_type': ProjectSourceTypeChoices.DATA_SOURCE})
        self.assertIn('data_source', form.fields)
        self.assertIn('data_path', form.fields)

    def test_source_ownership_fields_follow_instance_source_type(self):
        upload_project = CustomScriptProject.objects.create(name='CustomScriptProject 6', key='project-6')
        data_source = DataSource.objects.create(
            name='Data Source Form 2',
            type='local',
            source_url='file:///tmp/data-source-form-2/',
        )
        data_source_project = CustomScriptProject.objects.create(
            name='CustomScriptProject 7',
            key='project-7',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        self.assertNotIn('data_source', CustomScriptProjectEditForm(instance=upload_project).fields)
        self.assertIn('data_source', CustomScriptProjectEditForm(instance=data_source_project).fields)

    def test_source_type_uses_htmx_select(self):
        form = CustomScriptProjectEditForm()
        self.assertIsInstance(form.fields['source_type'].widget, HTMXSelect)


class CustomScriptProjectFilterFormTestCase(TestCase):
    def test_empty_filter_is_valid(self):
        form = CustomScriptProjectFilterForm(data={})
        self.assertTrue(form.is_valid(), form.errors)


class CustomScriptProjectBulkImportFormTestCase(TestCase):
    def test_good_path(self):
        form = CustomScriptProjectBulkImportForm(
            data={
                'name': 'CustomScriptProject Import 1',
                'key': 'project-import-1',
                'description': 'CSV-imported',
                'source_type': ProjectSourceTypeChoices.UPLOAD,
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_missing_required_field(self):
        form = CustomScriptProjectBulkImportForm(data={'description': 'No name or key'})
        self.assertFalse(form.is_valid())
        self.assertIn('name', form.errors)
        self.assertIn('key', form.errors)
