from django.db import IntegrityError, transaction
from django.test import TestCase

from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.constants import MAX_SCRIPT_CLASS_NAME_LENGTH, MAX_SCRIPT_MODULE_PATH_LENGTH
from netbox_custom_scripts.models import (
    CustomScript,
    CustomScriptModule,
    CustomScriptProject,
    CustomScriptProjectRevision,
)

DIGEST_A = 'a' * 64


class CustomScriptTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Script Project 1', key='script-project-1')
        cls.other_project = CustomScriptProject.objects.create(name='Script Project 2', key='script-project-2')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def _script(self, project=None, module_path='deploy', class_name='DeployDevices', **kwargs):
        return CustomScript.objects.create(
            project=project or self.project,
            module_path=module_path,
            class_name=class_name,
            display_name=kwargs.pop('display_name', 'Deploy Devices'),
            **kwargs,
        )

    def test_create_customscript(self):
        instance = self._script()
        self.assertIsNotNone(instance.pk)
        self.assertTrue(instance.enabled)
        self.assertFalse(instance.is_retired)
        self.assertIsNone(instance.last_seen_revision)
        self.assertEqual(instance.metadata, {})
        self.assertEqual(instance.description, '')

    def test_str(self):
        instance = CustomScript(project=self.project, module_path='deploy', class_name='X', display_name='Deploy')
        self.assertEqual(str(instance), 'Deploy')

    def test_full_name_joins_the_module_path_and_class_name(self):
        instance = CustomScript(project=self.project, module_path='tools.deploy', class_name='DeployDevices')
        self.assertEqual(instance.full_name, 'tools.deploy.DeployDevices')

    def test_the_system_managed_fields_are_not_editable(self):
        # editable=False is what keeps them off every form and makes DRF render them read-only.
        for field in ('display_name', 'description', 'is_retired', 'last_seen_revision', 'metadata'):
            with self.subTest(field=field):
                self.assertFalse(CustomScript._meta.get_field(field).editable)

    def test_enabled_stays_editable(self):
        # The administrator owns it, so no synchronization may take it over.
        self.assertTrue(CustomScript._meta.get_field('enabled').editable)

    def test_the_identity_fields_declare_the_validated_bounds(self):
        # Validation rejects an over-long identity while it still owns a verdict, which only
        # holds while the model and the constants agree.
        self.assertEqual(CustomScript._meta.get_field('class_name').max_length, MAX_SCRIPT_CLASS_NAME_LENGTH)
        self.assertEqual(CustomScript._meta.get_field('module_path').max_length, MAX_SCRIPT_MODULE_PATH_LENGTH)

    def test_the_duplicate_identity_is_refused_by_the_database(self):
        self._script()
        with transaction.atomic(), self.assertRaises(IntegrityError):
            self._script()

    def test_the_same_identity_is_allowed_on_another_project(self):
        first = self._script()
        second = self._script(project=self.other_project)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.full_name, second.full_name)

    def test_a_different_class_in_the_same_module_is_a_separate_identity(self):
        self._script()
        other = self._script(class_name='DecommissionDevices')
        self.assertIsNotNone(other.pk)

    def test_is_executable_when_enabled_and_published(self):
        self.assertTrue(self._script().is_executable)

    def test_is_not_executable_when_disabled(self):
        self.assertFalse(self._script(enabled=False).is_executable)

    def test_is_not_executable_when_retired(self):
        self.assertFalse(self._script(is_retired=True).is_executable)

    def test_is_not_executable_when_the_project_is_disabled(self):
        self.other_project.enabled = False
        self.other_project.save()
        self.assertFalse(self._script(project=self.other_project).is_executable)

    def test_a_description_longer_than_the_inherited_bound_round_trips(self):
        # The model overrides the abstract base CharField with a TextField, so the authoring
        # API's unbounded Meta.description needs no truncation.
        description = 'd' * 500
        instance = self._script(description=description)
        instance.refresh_from_db()
        self.assertEqual(instance.description, description)

    def test_deleting_a_module_leaves_the_scripts_intact(self):
        # A script's publishing entrypoint is provenance, not a relational parent.
        module = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        instance = self._script()
        module.delete()
        instance.refresh_from_db()
        self.assertIsNotNone(instance.pk)

    def test_deleting_the_project_cascades_the_scripts(self):
        project = CustomScriptProject.objects.create(name='Script Project 3', key='script-project-3')
        self._script(project=project)
        project.delete()
        self.assertFalse(CustomScript.objects.filter(project_id=project.pk).exists())

    def test_retirement_preserves_the_primary_key(self):
        # Retirement replaces deletion so the Job history attached to the row survives.
        instance = self._script()
        original_pk = instance.pk
        instance.is_retired = True
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.pk, original_pk)
        self.assertTrue(instance.is_retired)

    def test_last_seen_revision_round_trips(self):
        instance = self._script(last_seen_revision=self.revision)
        instance.refresh_from_db()
        self.assertEqual(instance.last_seen_revision, self.revision)

    def test_a_script_survives_its_last_seen_revision_being_deleted(self):
        # SET_NULL rather than CASCADE, so pruning old revisions never takes the scripts with
        # them. The revision carries no digest because the deletion receiver skips storage
        # cleanup only for one that was never written, and a bare fixture has no real manifest.
        revision = CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=None,
            status=RevisionStatusChoices.INVALID,
        )
        instance = self._script(last_seen_revision=revision)
        revision.delete()
        instance.refresh_from_db()
        self.assertIsNotNone(instance.pk)
        self.assertIsNone(instance.last_seen_revision)
