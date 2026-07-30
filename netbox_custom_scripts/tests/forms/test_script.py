from django.test import TestCase

from netbox_custom_scripts.forms import CustomScriptBulkEditForm, CustomScriptEditForm
from netbox_custom_scripts.models import CustomScript, CustomScriptProject


class CustomScriptEditFormTestCase(TestCase):
    """The writable set, and that a stale save cannot revert a derived field."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Form Project', key='form-project')

    def setUp(self):
        # Built per test, not in setUpTestData: the stale-save test mutates the row out from
        # under the bound form, and a shared class-level fixture would leak that.
        self.script = CustomScript.objects.create(
            project=self.project,
            module_path='deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            metadata={'commit_default': True},
        )

    def test_the_administrator_fields_are_writable(self):
        form = CustomScriptEditForm(
            data={'enabled': False, 'comments': 'Paused pending review.'},
            instance=self.script,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.script.refresh_from_db()
        self.assertFalse(self.script.enabled)
        self.assertEqual(self.script.comments, 'Paused pending review.')

    def test_the_derived_fields_are_absent_from_the_form(self):
        form = CustomScriptEditForm(instance=self.script)
        for name in ('project', 'module_path', 'class_name', 'display_name', 'description', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertNotIn(name, form.fields)

    def test_a_stale_save_does_not_revert_a_derived_field(self):
        # The form binds to the row as it was, then a concurrent synchronization renames it.
        # Saving must not write the stale display_name back.
        form = CustomScriptEditForm(data={'enabled': False}, instance=self.script)
        self.assertTrue(form.is_valid(), form.errors)

        CustomScript.objects.filter(pk=self.script.pk).update(display_name='Renamed By Sync')
        form.save()

        self.script.refresh_from_db()
        self.assertEqual(self.script.display_name, 'Renamed By Sync')
        self.assertFalse(self.script.enabled)

    def test_an_uncommitted_save_writes_nothing(self):
        form = CustomScriptEditForm(data={'enabled': False}, instance=self.script)
        self.assertTrue(form.is_valid(), form.errors)
        form.save(commit=False)

        self.script.refresh_from_db()
        self.assertTrue(self.script.enabled)


class CustomScriptBulkEditFormTestCase(TestCase):
    """The bulk form offers the administrator's field and nothing synchronization owns."""

    def test_enabled_is_the_only_offered_attribute(self):
        form = CustomScriptBulkEditForm()
        self.assertIn('enabled', form.fields)
        for name in ('module_path', 'class_name', 'display_name', 'description', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertNotIn(name, form.fields)

    def test_description_is_not_nullable(self):
        # description is editable=False and synchronization owns it, so a bulk null would
        # fight the next activation.
        self.assertNotIn('description', CustomScriptBulkEditForm.nullable_fields)
