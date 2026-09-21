from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils.timezone import now as local_now

from netbox_scripts.activation import synchronize_scripts
from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.constants import (
    DISCOVERED_SCRIPT_FILE_FIELDS,
    MAX_SCRIPT_CLASS_NAME_LENGTH,
    MAX_SCRIPT_MODULE_PATH_LENGTH,
    PUBLISHED_SCRIPT_FIELDS,
)
from netbox_scripts.models import (
    NetBoxScript,
    ScriptFile,
    ScriptProject,
    ScriptProjectRevision,
)
from netbox_scripts.utils import source_path_to_dotted_name
from netbox_scripts.validation import _persist_script_file_results

DIGEST_A = 'a' * 64
DIGEST_B = 'b' * 64


class NetBoxScriptTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Script Project 1', key='script-project-1')
        cls.other_project = ScriptProject.objects.create(name='Script Project 2', key='script-project-2')
        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.ACTIVE,
        )
        cls.other_revision = ScriptProjectRevision.objects.create(
            project=cls.other_project,
            digest=DIGEST_B,
            status=RevisionStatusChoices.ACTIVE,
        )
        # A script is only executable while its project is serving a revision, so both projects
        # start out serving one and each test takes away whatever it is about.
        ScriptProject.objects.filter(pk=cls.project.pk).update(active_revision=cls.revision)
        ScriptProject.objects.filter(pk=cls.other_project.pk).update(active_revision=cls.other_revision)
        cls.project.refresh_from_db()
        cls.other_project.refresh_from_db()

    def _script(self, project=None, module_path='deploy', class_name='DeployDevices', **kwargs):
        return NetBoxScript.objects.create(
            project=project or self.project,
            module_path=module_path,
            class_name=class_name,
            display_name=kwargs.pop('display_name', 'Deploy Devices'),
            **kwargs,
        )

    def test_create_netboxscript(self):
        instance = self._script()
        self.assertIsNotNone(instance.pk)
        self.assertTrue(instance.enabled)
        self.assertFalse(instance.is_retired)
        self.assertIsNone(instance.last_seen_revision)
        self.assertEqual(instance.metadata, {})
        self.assertEqual(instance.description, '')

    def test_str(self):
        instance = NetBoxScript(project=self.project, module_path='deploy', class_name='X', display_name='Deploy')
        self.assertEqual(str(instance), 'Deploy')

    def test_full_name_joins_the_module_path_and_class_name(self):
        instance = NetBoxScript(project=self.project, module_path='tools.deploy', class_name='DeployDevices')
        self.assertEqual(instance.full_name, 'tools.deploy.DeployDevices')

    def test_the_system_managed_fields_are_not_editable(self):
        # editable=False is what keeps them off every form and makes DRF render them read-only.
        for field in ('display_name', 'description', 'is_retired', 'last_seen_revision', 'metadata'):
            with self.subTest(field=field):
                self.assertFalse(NetBoxScript._meta.get_field(field).editable)

    def test_enabled_stays_editable(self):
        # The administrator owns it, so no synchronization may take it over.
        self.assertTrue(NetBoxScript._meta.get_field('enabled').editable)

    def test_the_identity_fields_declare_the_validated_bounds(self):
        # Validation rejects an over-long identity while it still owns a verdict, which only
        # holds while the model and the constants agree.
        self.assertEqual(NetBoxScript._meta.get_field('class_name').max_length, MAX_SCRIPT_CLASS_NAME_LENGTH)
        self.assertEqual(NetBoxScript._meta.get_field('module_path').max_length, MAX_SCRIPT_MODULE_PATH_LENGTH)

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
        ScriptProject.objects.filter(pk=self.other_project.pk).update(active_revision=None)
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

    def test_deleting_a_script_file_leaves_the_scripts_intact(self):
        # A script's publishing script file is provenance, not a relational parent.
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance = self._script()
        script_file.delete()
        instance.refresh_from_db()
        self.assertIsNotNone(instance.pk)

    def test_deleting_the_project_cascades_the_scripts(self):
        project = ScriptProject.objects.create(name='Script Project 3', key='script-project-3')
        self._script(project=project)
        project.delete()
        self.assertFalse(NetBoxScript.objects.filter(project_id=project.pk).exists())

    def test_retirement_preserves_the_primary_key(self):
        # Retirement replaces deletion so the Job history attached to the row survives.
        instance = self._script()
        original_pk = instance.pk
        instance.is_retired = True
        instance.save(update_fields=('is_retired',))
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
        revision = ScriptProjectRevision.objects.create(
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

    def test_run_settings_resolve_like_the_accessors(self):
        instance = self._script(
            metadata={'job_timeout': 600, 'notifications_default': 'on_failure'}, job_timeout_override=30
        )
        self.assertEqual(instance.run_settings(), (30, 'on_failure'))
        self.assertEqual(instance.run_settings(notifications='never'), (30, 'never'))
        self.assertEqual(self._script(class_name='Bare').run_settings(), (None, 'always'))

    def test_run_settings_refuse_a_malformed_recorded_timeout(self):
        instance = self._script(metadata={'job_timeout': 'not-a-duration'})
        with self.assertRaises(ValidationError) as caught:
            instance.run_settings()
        self.assertIn('job timeout', str(caught.exception))
        self.assertIn('execution settings', str(caught.exception))

    def test_run_settings_refuse_a_malformed_recorded_notification_policy(self):
        instance = self._script(metadata={'notifications_default': 'not-a-policy'})
        with self.assertRaises(ValidationError):
            instance.run_settings()

    def test_run_settings_refuse_a_malformed_override(self):
        # The form and the serializer validate the override columns, so only an ORM write gets here.
        instance = self._script()
        NetBoxScript.objects.filter(pk=instance.pk).update(job_timeout_override=0)
        instance.refresh_from_db()
        with self.assertRaises(ValidationError):
            instance.run_settings()

    def test_the_timeout_phrase_reports_a_malformed_value_rather_than_raising(self):
        # Reached by clicking through from a run that was just refused.
        instance = self._script(metadata={'job_timeout': 'not-a-duration'})
        self.assertIn('not-a-duration', instance.job_timeout_display)

    def test_the_overrides_are_editable_like_enabled(self):
        fields = {field.name: field for field in NetBoxScript._meta.get_fields()}
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

    def test_a_stale_save_does_not_revert_a_published_field(self):
        # An activation publishing between the load and the save.
        script = self._script()
        loaded = NetBoxScript.objects.get(pk=script.pk)
        NetBoxScript.objects.filter(pk=script.pk).update(display_name='Renamed By Sync')

        loaded.comments = 'An administrator note.'
        loaded.save()

        loaded.refresh_from_db()
        self.assertEqual(loaded.display_name, 'Renamed By Sync')
        self.assertEqual(loaded.comments, 'An administrator note.')

    def test_a_stale_save_does_not_clear_retirement(self):
        script = self._script()
        loaded = NetBoxScript.objects.get(pk=script.pk)
        NetBoxScript.objects.filter(pk=script.pk).update(is_retired=True)

        loaded.enabled = False
        loaded.save()

        loaded.refresh_from_db()
        self.assertTrue(loaded.is_retired)
        self.assertFalse(loaded.enabled)

    def test_a_field_limited_publication_write_still_lands(self):
        # synchronize_scripts writes this way, so the guard must let it through.
        script = self._script()
        script.display_name = 'Published By Sync'
        script.save(update_fields=('display_name', 'last_updated'))

        script.refresh_from_db()
        self.assertEqual(script.display_name, 'Published By Sync')

    def test_the_protected_field_set_matches_what_synchronization_writes(self):
        # A field synchronization starts writing that the constant does not name would go
        # unprotected in silence, so this compares against what one real sync wrote.
        script = self._script(display_name='Before', description='Before', metadata={})
        NetBoxScript.objects.filter(pk=script.pk).update(is_retired=True)
        revision = ScriptProjectRevision.objects.create(
            project=self.project, digest='c' * 64, status=RevisionStatusChoices.VALID
        )
        script.refresh_from_db()
        before = {field.attname: getattr(script, field.attname) for field in NetBoxScript._meta.concrete_fields}

        synchronize_scripts(
            project=self.project,
            revision=revision,
            records=[
                {
                    'module_path': script.module_path,
                    'class_name': script.class_name,
                    'display_name': 'After',
                    'description': 'After',
                    'metadata': {'job_timeout': 60},
                }
            ],
            using=DEFAULT_DB_ALIAS,
        )

        script.refresh_from_db()
        after = {field.attname: getattr(script, field.attname) for field in NetBoxScript._meta.concrete_fields}
        written = {name for name, value in after.items() if before[name] != value} - {'last_updated'}
        self.assertEqual(written, set(PUBLISHED_SCRIPT_FIELDS))


class SourcePathToDottedNameTestCase(TestCase):
    def test_maps_paths_to_dotted_names(self):
        for path, dotted in (
            ('deploy.py', 'deploy'),
            ('tools/deploy.py', 'tools.deploy'),
            ('a/b/c.py', 'a.b.c'),
            ('pkg/__init__.py', 'pkg'),
            ('pkg/sub/__init__.py', 'pkg.sub'),
        ):
            with self.subTest(path=path):
                self.assertEqual(source_path_to_dotted_name(path), dotted)

    def test_rejects_a_non_python_file(self):
        for path in ('README.md', 'deploy', 'deploy.pyc'):
            with self.subTest(path=path), self.assertRaises(ValidationError):
                source_path_to_dotted_name(path)

    def test_rejects_the_root_init(self):
        with self.assertRaises(ValidationError) as cm:
            source_path_to_dotted_name('__init__.py')
        self.assertIn('project package itself', str(cm.exception))

    def test_rejects_an_invalid_identifier_segment(self):
        for path in ('my-tools/deploy.py', '1tools/deploy.py', 'tools/de ploy.py'):
            with self.subTest(path=path), self.assertRaises(ValidationError) as cm:
                source_path_to_dotted_name(path)
            self.assertIn('not a valid Python identifier', str(cm.exception))

    def test_rejects_a_reserved_keyword_segment(self):
        for path in ('class/deploy.py', 'tools/import.py'):
            with self.subTest(path=path), self.assertRaises(ValidationError) as cm:
                source_path_to_dotted_name(path)
            self.assertIn('reserved Python keyword', str(cm.exception))


class ScriptFileTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Script File Project 1', key='script-file-project-1')
        cls.other_project = ScriptProject.objects.create(name='Script File Project 2', key='script-file-project-2')

    def test_create_scriptfile(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        self.assertIsNotNone(instance.pk)
        self.assertTrue(instance.enabled)
        self.assertEqual(instance.discovery_status, FileDiscoveryStatusChoices.PENDING)
        self.assertEqual(instance.discovery_error, '')
        self.assertIsNone(instance.last_discovered_revision)

    def test_str(self):
        instance = ScriptFile(project=self.project, source_path='tools/deploy.py')
        self.assertEqual(str(instance), 'Script File Project 1: tools/deploy.py')

    def test_the_discovery_fields_are_system_managed(self):
        # editable=False is what keeps them off every form and serializer.
        for field in ('discovery_status', 'discovery_error', 'last_discovered_revision'):
            with self.subTest(field=field):
                self.assertFalse(ScriptFile._meta.get_field(field).editable)

    def test_clean_canonicalizes_the_source_path(self):
        instance = ScriptFile(project=self.project, source_path='./tools//deploy.py')
        instance.full_clean()
        self.assertEqual(instance.source_path, 'tools/deploy.py')

    def test_save_canonicalizes_the_source_path(self):
        # Snapshot building reads rows straight from the ORM, so canonical form must hold
        # even for writes that never ran full_clean().
        instance = ScriptFile.objects.create(project=self.project, source_path='./tools/./deploy.py')
        self.assertEqual(instance.source_path, 'tools/deploy.py')

    def test_rejects_an_unsafe_source_path(self):
        for path in ('../outside.py', '/etc/deploy.py'):
            with self.subTest(path=path):
                instance = ScriptFile(project=self.project, source_path=path)
                with self.assertRaises(ValidationError) as cm:
                    instance.full_clean()
                self.assertIn('source_path', cm.exception.message_dict)

    def test_save_rejects_an_unsafe_source_path(self):
        instance = ScriptFile(project=self.project, source_path='../outside.py')
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_rejects_a_path_that_cannot_be_imported(self):
        instance = ScriptFile(project=self.project, source_path='my-tools/deploy.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_rejects_a_case_folded_sibling_on_the_same_project(self):
        ScriptFile.objects.create(project=self.project, source_path='Utils.py')
        instance = ScriptFile(project=self.project, source_path='utils.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('differ only in letter case', str(cm.exception.message_dict['source_path']))

    def test_rejects_a_sibling_colliding_only_in_a_parent_directory(self):
        # The manifest builder rejects this source tree, so accepting the declaration pair
        # would leave the author with an upload error naming files rather than declarations.
        ScriptFile.objects.create(project=self.project, source_path='Lib/deploy.py')
        instance = ScriptFile(project=self.project, source_path='lib/audit.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        message = str(cm.exception.message_dict['source_path'])
        self.assertIn('differ only in letter case', message)
        self.assertIn('Lib', message)

    def test_allows_a_sibling_only_full_caseless_folding_would_merge(self):
        ScriptFile.objects.create(project=self.project, source_path='strasse.py')
        instance = ScriptFile(project=self.project, source_path='straße.py')
        instance.full_clean()
        instance.save()
        self.assertIsNotNone(instance.pk)

    def test_rejects_a_sibling_importing_under_the_same_module_name(self):
        # "pkg.py" and "pkg/__init__.py" both import as "pkg", so only one could ever run.
        ScriptFile.objects.create(project=self.project, source_path='pkg/__init__.py')
        instance = ScriptFile(project=self.project, source_path='pkg.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('the same module name', str(cm.exception.message_dict['source_path']))

    def test_the_sibling_scan_reads_only_the_paths_it_compares(self):
        # A full-row fetch drags discovery_error, an unbounded field, across every sibling.
        ScriptFile.objects.create(project=self.project, source_path='utils.py')
        instance = ScriptFile(project=self.project, source_path='deploy.py')

        with CaptureQueriesContext(connection) as captured:
            instance.full_clean()

        scans = [entry['sql'] for entry in captured.captured_queries if 'source_path' in entry['sql']]
        self.assertTrue(scans)
        for sql in scans:
            self.assertNotIn('discovery_error', sql)
            self.assertNotIn('discovery_status', sql)

    def test_save_refuses_a_case_folded_sibling_without_full_clean(self):
        ScriptFile.objects.create(project=self.project, source_path='Utils.py')
        with self.assertRaises(ValidationError) as cm:
            ScriptFile.objects.create(project=self.project, source_path='utils.py')
        self.assertIn('differ only in letter case', str(cm.exception.message_dict['source_path']))

    def test_save_refuses_an_unimportable_path_without_full_clean(self):
        instance = ScriptFile(project=self.project, source_path='lib/data-helper.py')
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_path', cm.exception.message_dict)
        self.assertFalse(ScriptFile.objects.filter(project=self.project).exists())

    def test_allows_the_same_path_on_another_project(self):
        ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance = ScriptFile(project=self.other_project, source_path='deploy.py')
        instance.full_clean()
        instance.save()
        self.assertIsNotNone(instance.pk)

    def test_editing_a_script_file_does_not_collide_with_itself(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.description = 'Edited by the test suite.'
        instance.full_clean()

    def test_source_path_is_immutable(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.source_path = 'renamed.py'
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_save_refuses_a_changed_source_path(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.source_path = 'renamed.py'
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_project_is_immutable(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.project = self.other_project
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('project', cm.exception.message_dict)

    def test_save_refuses_a_changed_project(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.project = self.other_project
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('project', cm.exception.message_dict)

    def test_a_respelled_source_path_is_not_a_change(self):
        # Canonicalization runs before the comparison, so the same file spelled differently
        # is accepted rather than read as a rename.
        instance = ScriptFile.objects.create(project=self.project, source_path='tools/deploy.py')
        instance.source_path = './tools//deploy.py'
        instance.full_clean()
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.source_path, 'tools/deploy.py')

    def test_the_editable_fields_still_change(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.enabled = False
        instance.description = 'Suspended by the test suite.'
        instance.full_clean()
        instance.save()
        instance.refresh_from_db()
        self.assertFalse(instance.enabled)
        self.assertEqual(instance.description, 'Suspended by the test suite.')

    def test_the_exact_duplicate_is_refused_by_the_database(self):
        ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        # bulk_create skips save(), so this reaches the constraint rather than the model guard.
        with self.assertRaises(IntegrityError), transaction.atomic():
            ScriptFile.objects.bulk_create([ScriptFile(project=self.project, source_path='deploy.py')])

    def test_last_discovered_revision_must_belong_to_the_project(self):
        foreign = ScriptProjectRevision.objects.create(
            project=self.other_project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.last_discovered_revision = foreign
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('last_discovered_revision', cm.exception.message_dict)

    def test_last_discovered_revision_of_the_same_project_is_accepted(self):
        revision = ScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.last_discovered_revision = revision
        instance.full_clean()
        instance.save(update_fields=('last_discovered_revision',))
        instance.refresh_from_db()
        self.assertEqual(instance.last_discovered_revision, revision)

    def test_a_stale_save_does_not_revert_a_discovery_result(self):
        # A discovery result landing between the load and the save.
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        loaded = ScriptFile.objects.get(pk=instance.pk)
        ScriptFile.objects.filter(pk=instance.pk).update(
            discovery_status=FileDiscoveryStatusChoices.FAILED,
            discovery_error='It did not import.',
        )

        loaded.enabled = False
        loaded.save()

        loaded.refresh_from_db()
        self.assertEqual(loaded.discovery_status, FileDiscoveryStatusChoices.FAILED)
        self.assertEqual(loaded.discovery_error, 'It did not import.')
        self.assertFalse(loaded.enabled)

    def test_a_field_limited_discovery_write_still_lands(self):
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        instance.discovery_status = FileDiscoveryStatusChoices.FAILED
        instance.save(update_fields=('discovery_status',))

        instance.refresh_from_db()
        self.assertEqual(instance.discovery_status, FileDiscoveryStatusChoices.FAILED)

    def test_the_protected_field_set_matches_what_discovery_writes(self):
        # A field discovery starts writing that the constant does not name would go unprotected
        # in silence, so this compares against what one real write recorded.
        instance = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        revision = ScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.VALIDATING,
            # The writer compares against this, so a claim without one cannot be recorded.
            validation_started=local_now(),
        )
        before = {field.attname: getattr(instance, field.attname) for field in ScriptFile._meta.concrete_fields}

        _persist_script_file_results(
            revision,
            [{'source_path': 'deploy.py', 'script_file': instance.pk}],
            {'deploy.py': FileDiscoveryStatusChoices.FAILED},
            [{'source_path': 'deploy.py', 'message': 'It did not import.'}],
            {},
        )

        instance.refresh_from_db()
        after = {field.attname: getattr(instance, field.attname) for field in ScriptFile._meta.concrete_fields}
        written = {name for name, value in after.items() if before[name] != value} - {'last_updated'}
        self.assertEqual(written, set(DISCOVERED_SCRIPT_FILE_FIELDS))
