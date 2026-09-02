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
DIGEST_B = 'b' * 64


class CustomScriptTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Script Project 1', key='script-project-1')
        cls.other_project = CustomScriptProject.objects.create(name='Script Project 2', key='script-project-2')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.ACTIVE,
        )
        cls.other_revision = CustomScriptProjectRevision.objects.create(
            project=cls.other_project,
            digest=DIGEST_B,
            status=RevisionStatusChoices.ACTIVE,
        )
        # A script is only executable while its project is serving a revision, so both projects
        # start out serving one and each test takes away whatever it is about.
        CustomScriptProject.objects.filter(pk=cls.project.pk).update(active_revision=cls.revision)
        CustomScriptProject.objects.filter(pk=cls.other_project.pk).update(active_revision=cls.other_revision)
        cls.project.refresh_from_db()
        cls.other_project.refresh_from_db()

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
        script = self._script()
        self.assertTrue(script.is_executable)
        self.assertIsNone(script.run_refusal_reason)

    def test_is_not_executable_when_disabled(self):
        script = self._script(enabled=False)
        self.assertFalse(script.is_executable)
        self.assertEqual(str(script.run_refusal_reason), 'It is disabled.')

    def test_is_not_executable_when_retired(self):
        script = self._script(is_retired=True)
        self.assertFalse(script.is_executable)
        self.assertEqual(str(script.run_refusal_reason), 'It is retired, so its Project no longer publishes it.')

    def test_is_not_executable_when_the_project_is_disabled(self):
        self.other_project.enabled = False
        self.other_project.save()
        script = self._script(project=self.other_project)
        self.assertFalse(script.is_executable)
        self.assertEqual(str(script.run_refusal_reason), 'Its Project is disabled.')

    def test_is_not_executable_when_the_project_serves_no_revision(self):
        # Deactivation retires every script in the same transaction, so this state is normally
        # unreachable. The check makes the guarantee local rather than an agreement between two
        # code paths, which is what execution has to be able to trust.
        CustomScriptProject.objects.filter(pk=self.other_project.pk).update(active_revision=None)
        self.other_project.refresh_from_db()
        script = self._script(project=self.other_project)
        self.assertFalse(script.is_executable)
        # The condition every surface used to omit. Reached by deleting the active revision,
        # which SET_NULLs the pointer and leaves the scripts unretired.
        self.assertEqual(str(script.run_refusal_reason), 'Its Project is serving no revision.')

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

    def test_execution_defaults_fall_back_when_metadata_is_empty(self):
        instance = self._script()
        self.assertTrue(instance.commit_default)
        self.assertTrue(instance.scheduling_enabled)
        self.assertIsNone(instance.job_timeout)
        self.assertEqual(instance.notifications_default, 'always')

    def test_execution_defaults_read_what_validation_recorded(self):
        instance = self._script(
            metadata={
                'commit_default': False,
                'scheduling_enabled': False,
                'job_timeout': 600,
                'notifications_default': 'on_failure',
            }
        )
        self.assertFalse(instance.commit_default)
        self.assertFalse(instance.scheduling_enabled)
        self.assertEqual(instance.job_timeout, 600)
        self.assertEqual(instance.notifications_default, 'on_failure')

    def test_an_override_wins_over_what_validation_recorded(self):
        instance = self._script(
            metadata={
                'commit_default': False,
                'scheduling_enabled': False,
                'job_timeout': 600,
                'notifications_default': 'on_failure',
            },
            commit_default_override=True,
            job_timeout_override=30,
            notifications_default_override='never',
        )
        self.assertTrue(instance.commit_default)
        self.assertEqual(instance.job_timeout, 30)
        self.assertEqual(instance.notifications_default, 'never')
        # The author's safety claim takes no override, so it still reads what the class declared.
        self.assertFalse(instance.scheduling_enabled)

    def test_an_override_applies_with_no_recorded_value_to_override(self):
        instance = self._script(
            commit_default_override=False,
            job_timeout_override=45,
            notifications_default_override='on_failure',
        )
        self.assertFalse(instance.commit_default)
        self.assertEqual(instance.job_timeout, 45)
        self.assertEqual(instance.notifications_default, 'on_failure')

    def test_a_false_commit_override_is_applied_rather_than_read_as_unset(self):
        # False is a value here and empty is the absence of one, which is why the column is
        # nullable and the accessor tests against None.
        instance = self._script(metadata={'commit_default': True}, commit_default_override=False)
        self.assertFalse(instance.commit_default)

    def test_an_unset_override_inherits_rather_than_forcing_the_system_default(self):
        # The documented boundary of the nullable column: empty means inherit, so an operator
        # cannot override a declared timeout back to the system default and sets a number instead.
        instance = self._script(metadata={'job_timeout': 600})
        self.assertIsNone(instance.job_timeout_override)
        self.assertEqual(instance.job_timeout, 600)

    def test_the_overrides_are_editable_like_enabled(self):
        fields = {field.name: field for field in CustomScript._meta.get_fields()}
        for name in ('commit_default_override', 'job_timeout_override', 'notifications_default_override'):
            self.assertTrue(fields[name].editable, name)

    def test_the_timeout_phrase_follows_the_override(self):
        self.assertEqual(
            self._script(metadata={'job_timeout': 600}, job_timeout_override=1).job_timeout_display,
            '1 second',
        )

    def test_the_timeout_reads_as_a_phrase_rather_than_a_bare_number(self):
        # No timeout is a real setting, not missing data, so it must not render as a placeholder.
        self.assertEqual(self._script().job_timeout_display, 'System default')
        self.assertEqual(self._script(class_name='B', metadata={'job_timeout': 600}).job_timeout_display, '600 seconds')
        self.assertEqual(self._script(class_name='C', metadata={'job_timeout': 1}).job_timeout_display, '1 second')

    def test_the_notification_policy_reads_as_its_label(self):
        self.assertEqual(self._script().get_notifications_default_display(), 'Always')
        self.assertEqual(
            self._script(
                class_name='B', metadata={'notifications_default': 'on_failure'}
            ).get_notifications_default_display(),
            'On failure',
        )
