from django.test import TestCase

from netbox_scripts.forms import (
    NetBoxScriptBulkEditForm,
    NetBoxScriptEditForm,
    ScriptFileEditForm,
    ScriptFileFilterForm,
)
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject


class NetBoxScriptEditFormTestCase(TestCase):
    """The writable set, and that a stale save cannot revert a derived field."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Form Project', key='form-project')

    def setUp(self):
        # Built per test, not in setUpTestData: the stale-save test mutates the row out from
        # under the bound form, and a shared class-level fixture would leak that.
        self.script = NetBoxScript.objects.create(
            project=self.project,
            module_path='deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            metadata={'commit_default': True},
        )

    def test_the_administrator_fields_are_writable(self):
        form = NetBoxScriptEditForm(
            data={'enabled': False, 'comments': 'Paused pending review.'},
            instance=self.script,
        )
        self.assertTrue(form.is_valid(), form.errors)
        form.save()

        self.script.refresh_from_db()
        self.assertFalse(self.script.enabled)
        self.assertEqual(self.script.comments, 'Paused pending review.')

    def test_the_derived_fields_are_absent_from_the_form(self):
        form = NetBoxScriptEditForm(instance=self.script)
        for name in ('project', 'module_path', 'class_name', 'display_name', 'description', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertNotIn(name, form.fields)

    def test_the_execution_overrides_are_writable_and_clearable(self):
        form = NetBoxScriptEditForm(
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
        cleared = NetBoxScriptEditForm(data={'enabled': True}, instance=self.script)
        self.assertTrue(cleared.is_valid(), cleared.errors)
        cleared.save()

        self.script.refresh_from_db()
        self.assertIsNone(self.script.commit_default_override)
        self.assertIsNone(self.script.job_timeout_override)
        self.assertEqual(self.script.notifications_default_override, '')


class NetBoxScriptBulkEditFormTestCase(TestCase):
    """The bulk form offers the administrator's field and nothing synchronization owns."""

    def test_the_administrator_fields_are_offered_and_nothing_derived_is(self):
        form = NetBoxScriptBulkEditForm()
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
                self.assertIn(name, NetBoxScriptBulkEditForm.nullable_fields)

    def test_description_is_not_nullable(self):
        # description is editable=False and synchronization owns it, so a bulk null would
        # fight the next activation.
        self.assertNotIn('description', NetBoxScriptBulkEditForm.nullable_fields)


class ScriptFileEditFormTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Form Project 1', key='form-project-1')
        cls.other_project = ScriptProject.objects.create(name='Form Project 2', key='form-project-2')

    def test_good_path(self):
        form = ScriptFileEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'tools/deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_project_is_required(self):
        form = ScriptFileEditForm(data={'source_path': 'deploy.py'})
        self.assertFalse(form.is_valid())
        self.assertIn('project', form.errors)

    def test_source_path_is_required(self):
        form = ScriptFileEditForm(data={'project': self.project.pk})
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_canonicalizes_the_source_path(self):
        form = ScriptFileEditForm(
            data={
                'project': self.project.pk,
                'source_path': './tools//deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.source_path, 'tools/deploy.py')

    def test_rejects_an_unimportable_path(self):
        form = ScriptFileEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'tools/deploy.txt',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_rejects_a_traversal_path(self):
        form = ScriptFileEditForm(
            data={
                'project': self.project.pk,
                'source_path': '../outside.py',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_rejects_a_case_folded_sibling(self):
        ScriptFile.objects.create(project=self.project, source_path='tools/deploy.py')
        form = ScriptFileEditForm(
            data={
                'project': self.project.pk,
                'source_path': 'Tools/Deploy.py',
                'enabled': True,
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn('source_path', form.errors)

    def test_accepts_the_same_path_on_another_project(self):
        ScriptFile.objects.create(project=self.project, source_path='tools/deploy.py')
        form = ScriptFileEditForm(
            data={
                'project': self.other_project.pk,
                'source_path': 'tools/deploy.py',
                'enabled': True,
            }
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_discovery_fields_are_not_form_fields(self):
        form = ScriptFileEditForm()
        for field in ('discovery_status', 'discovery_error', 'last_discovered_revision'):
            with self.subTest(field=field):
                self.assertNotIn(field, form.fields)

    def test_tags_included_in_fieldsets(self):
        field_names = [item for fieldset in ScriptFileEditForm.fieldsets for item in fieldset.items]
        self.assertIn('tags', field_names)


class ScriptFileFilterFormTestCase(TestCase):
    def test_empty_filter_is_valid(self):
        form = ScriptFileFilterForm(data={})
        self.assertTrue(form.is_valid(), form.errors)
