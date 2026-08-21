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

    def test_the_execution_overrides_are_writable_and_clearable(self):
        form = CustomScriptEditForm(
            data={
                'enabled': True,
                'commit_default_override': False,
                'job_timeout_override': 45,
                'notifications_default_override': 'never',
            },
            instance=self.script,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.script.refresh_from_db()
        self.assertIs(self.script.commit_default_override, False)
        self.assertEqual(self.script.job_timeout_override, 45)
        self.assertEqual(self.script.notifications_default_override, 'never')

        # Clearing has to reach the row as well, or an override could be set and never removed.
        cleared = CustomScriptEditForm(data={'enabled': True}, instance=self.script)
        self.assertTrue(cleared.is_valid(), cleared.errors)
        cleared.save()

        self.script.refresh_from_db()
        self.assertIsNone(self.script.commit_default_override)
        self.assertIsNone(self.script.job_timeout_override)
        self.assertEqual(self.script.notifications_default_override, '')


class CustomScriptBulkEditFormTestCase(TestCase):
    """The bulk form offers the administrator's field and nothing synchronization owns."""

    def test_the_administrator_fields_are_offered_and_nothing_derived_is(self):
        form = CustomScriptBulkEditForm()
        for name in (
            'enabled',
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
        ):
            self.assertIn(name, form.fields)
        for name in ('module_path', 'class_name', 'display_name', 'description', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertNotIn(name, form.fields)

    def test_every_override_is_nullable_so_bulk_edit_can_clear_one(self):
        # nullable_fields is the only route back to inherit in bulk, since an empty bulk field
        # otherwise means "leave alone".
        for name in ('commit_default_override', 'job_timeout_override', 'notifications_default_override'):
            with self.subTest(field=name):
                self.assertIn(name, CustomScriptBulkEditForm.nullable_fields)

    def test_description_is_not_nullable(self):
        # description is editable=False and synchronization owns it, so a bulk null would
        # fight the next activation.
        self.assertNotIn('description', CustomScriptBulkEditForm.nullable_fields)
