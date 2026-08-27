import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone
from rq.timeouts import JobTimeoutException

from core.choices import JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job
from netbox_custom_scripts import jobs, validation
from netbox_custom_scripts.choices import ModuleDiscoveryStatusChoices, RevisionStatusChoices
from netbox_custom_scripts.constants import VALIDATION_JOB_TIMEOUT, VALIDATION_LEASE_SECONDS
from netbox_custom_scripts.jobs import RevisionValidationJob
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.runtime.exceptions import DiscoveryError, EntrypointImportError
from netbox_custom_scripts.runtime.naming import PRIVATE_ROOT, revision_module_name
from netbox_custom_scripts.storage import service
from netbox_custom_scripts.storage.exceptions import RevisionCorruptError, StorageError
from netbox_custom_scripts.tests.runtime.test_cache import discard_tree
from netbox_custom_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_custom_scripts.validation import (
    ValidationStateError,
    _top_level_module_names,
    build_error_sanitizer,
    classify_entrypoint_error,
    validate_revision,
)


def script_source(class_name):
    """Return entrypoint source publishing one Script subclass."""
    return f'from netbox_custom_scripts.scripts import Script\n\n\nclass {class_name}(Script):\n    pass\n'.encode()


SCRIPT_FILES = {'deploy.py': script_source('Deploy')}


class ValidationTestMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.cache_root = Path(tempfile.mkdtemp())
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_custom_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        self.project = CustomScriptProject.objects.create(name='Validated Project', key='validated-project')
        self.job = self.make_job()

    def _purge_namespace(self):
        import sys

        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def make_job(self):
        return Job.objects.create(name='validation-test', job_id=uuid.uuid4())

    def declare(self, source_path, enabled=True):
        return CustomScriptModule.objects.create(project=self.project, source_path=source_path, enabled=enabled)

    def stage(self, files):
        revision, _ = service.stage_revision(self.project, files)
        return revision

    def backdate_lease(self, revision):
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            validation_started=timezone.now() - timedelta(seconds=2 * VALIDATION_LEASE_SECONDS)
        )


class ClaimTestCase(ValidationTestMixin, TestCase):
    def test_a_materialized_revision_is_claimed_and_validated(self):
        self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.validation_errors, [])
        self.assertEqual(result.validation_job, self.job)
        self.assertIsNotNone(result.validation_started)

    def test_only_a_materialized_revision_is_claimable(self):
        for status in (
            RevisionStatusChoices.STAGING,
            RevisionStatusChoices.STORAGE_FAILED,
            RevisionStatusChoices.VALID,
            RevisionStatusChoices.INVALID,
            RevisionStatusChoices.ACTIVE,
            RevisionStatusChoices.RETIRED,
        ):
            with self.subTest(status=status):
                revision = self.stage({'deploy.py': script_source(f'S{status.title()}')})
                CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(status=status)
                revision.refresh_from_db()
                with self.assertRaises(ValidationStateError):
                    validate_revision(revision, job=self.job)

    def test_a_live_lease_refuses_a_second_claim(self):
        revision = self.stage(SCRIPT_FILES)
        other = self.make_job()
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALIDATING, validation_job=other, validation_started=timezone.now()
        )
        revision.refresh_from_db()
        with self.assertRaises(ValidationStateError):
            validate_revision(revision, job=self.job)

    def test_an_expired_lease_is_reclaimed_even_while_the_old_job_row_says_running(self):
        self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        old = self.make_job()
        Job.objects.filter(pk=old.pk).update(status=JobStatusChoices.STATUS_RUNNING)
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALIDATING,
            validation_job=old,
            validation_started=timezone.now() - timedelta(seconds=2 * VALIDATION_LEASE_SECONDS),
        )
        revision.refresh_from_db()
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.validation_job, self.job)

    def test_a_stale_worker_resuming_after_a_reclaim_commits_nothing(self):
        module_row = self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        old_job = self.make_job()
        new_job = self.make_job()
        real_discover = validation.discover_scripts
        state = {'interleaved': False}

        def interleave(module, **kwargs):
            if state['interleaved']:
                return real_discover(module, **kwargs)
            state['interleaved'] = True
            # The first run pauses here holding a claim that then expires. A second
            # worker reclaims, validates VALID, and commits. The first run resumes into
            # a planted content failure, so any fence break would flip the verdict.
            self.backdate_lease(revision)
            validate_revision(CustomScriptProjectRevision.objects.get(pk=revision.pk), job=new_job)
            raise DiscoveryError('planted failure for the stale run', code='not_a_script')

        with mock.patch.object(validation, 'discover_scripts', side_effect=interleave):
            stale_result = validate_revision(revision, job=old_job)

        self.assertEqual(stale_result.status, RevisionStatusChoices.VALID)
        self.assertEqual(stale_result.validation_errors, [])
        self.assertEqual(stale_result.validation_job, new_job)
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(module_row.discovery_error, '')


class SnapshotTestCase(ValidationTestMixin, TestCase):
    def test_a_tampered_snapshot_fails_closed_and_releases_the_claim(self):
        self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            entrypoint_snapshot=[{'module': 1, 'source_path': 'deploy.py', 'extra': 'field'}]
        )
        revision.refresh_from_db()
        with self.assertRaises(RevisionCorruptError):
            validate_revision(revision, job=self.job)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertIsNone(revision.validation_job)
        self.assertIsNone(revision.validation_started)

    def test_the_verdict_follows_the_snapshot_not_live_module_rows(self):
        module_row = self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        module_row.delete()
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)

    def test_an_empty_snapshot_is_vacuously_valid(self):
        revision = self.stage(SCRIPT_FILES)
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.validation_errors, [])


class VerdictTestCase(ValidationTestMixin, TestCase):
    def test_two_entrypoints_validate_and_both_modules_discover(self):
        first = self.declare('deploy.py')
        second = self.declare('audit.py')
        revision = self.stage({'deploy.py': script_source('Deploy'), 'audit.py': script_source('Audit')})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        for module_row in (first, second):
            module_row.refresh_from_db()
            self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
            self.assertEqual(module_row.discovery_error, '')
            self.assertEqual(module_row.last_discovered_revision, result)

    def test_a_syntax_error_is_an_invalid_verdict_and_spares_the_sibling(self):
        good = self.declare('deploy.py')
        bad = self.declare('broken.py')
        revision = self.stage({'deploy.py': script_source('Deploy'), 'broken.py': b'def broken(:\n'})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        (record,) = result.validation_errors
        self.assertEqual(record['source_path'], 'broken.py')
        self.assertEqual(record['code'], 'entrypoint_import_failed')
        self.assertEqual(record['exception_type'], 'SyntaxError')
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(bad.discovery_status, ModuleDiscoveryStatusChoices.FAILED)
        # The sanitizer reduced the cache path in the message to the bare relative path.
        self.assertEqual(bad.discovery_error, 'invalid syntax (broken.py, line 1)')

    def test_project_code_raising_is_an_invalid_verdict(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'raise RuntimeError("refused")\n'})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual(result.validation_errors[0]['exception_type'], 'RuntimeError')

    def test_a_missing_revision_module_is_an_invalid_verdict(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'from .helpers import tool\n'})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)

    def test_a_missing_from_list_name_is_an_invalid_verdict(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'from . import missing\n'})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)

    def test_a_missing_external_distribution_reverts_and_reraises(self):
        module_row = self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'import package_that_is_not_installed_anywhere\n'})
        with self.assertRaises(EntrypointImportError):
            validate_revision(revision, job=self.job)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertIsNone(revision.validation_job)
        self.assertIsNone(revision.validation_started)
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.PENDING)

    def test_the_reason_a_verdict_could_not_be_reached_is_recorded(self):
        # The lease fields are given back, so this is the only thing that survives to say why a
        # revision returning to materialized will never reach a verdict on its own.
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'import package_that_is_not_installed_anywhere\n'})

        with self.assertRaises(EntrypointImportError):
            validate_revision(revision, job=self.job)

        revision.refresh_from_db()
        self.assertIn('package_that_is_not_installed_anywhere', revision.validation_error)

    def test_a_recorded_reason_is_cleared_when_a_verdict_is_reached(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'import package_that_is_not_installed_anywhere\n'})
        with self.assertRaises(EntrypointImportError):
            validate_revision(revision, job=self.job)
        revision.refresh_from_db()
        self.assertTrue(revision.validation_error)

        working = self.stage({'deploy.py': b'from netbox_custom_scripts.scripts import Script\n'})
        validate_revision(working, job=self.make_job())

        working.refresh_from_db()
        self.assertEqual(working.validation_error, '')

    def test_a_passthrough_exception_reverts_and_escapes_unwrapped(self):
        self.declare('deploy.py')
        revision = self.stage(
            {'deploy.py': b'from rq.timeouts import JobTimeoutException\nraise JobTimeoutException()\n'}
        )
        with self.assertRaises(JobTimeoutException):
            validate_revision(revision, job=self.job, passthrough=(JobTimeoutException,))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)

    def test_two_classes_sharing_one_identity_across_entries_are_invalid(self):
        self.declare('first.py')
        self.declare('second.py')
        helpers = (
            b'from netbox_custom_scripts.scripts import Script\n\n\n'
            b'class Sync(Script):\n    pass\n\n\n'
            b'FIRST = Sync\n\n\n'
            b'class Sync(Script):\n    pass\n'
        )
        revision = self.stage(
            {
                'helpers.py': helpers,
                'first.py': b'from .helpers import FIRST\n\nscript_order = [FIRST]\n',
                'second.py': b'from .helpers import Sync\n\nscript_order = [Sync]\n',
            }
        )
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        codes = {record['code'] for record in result.validation_errors}
        self.assertIn('duplicate_identity', codes)

    def test_one_class_reexported_by_two_entries_is_one_publication(self):
        self.declare('first.py')
        self.declare('second.py')
        files = {
            'helpers.py': script_source('Shared'),
            'first.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
            'second.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
        }
        result = validate_revision(self.stage(files), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)

    def test_stored_errors_never_carry_runtime_identities(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'raise RuntimeError(__file__)\n'})
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        serialized = json.dumps(result.validation_errors)
        storage_key = str(self.project.storage_key)
        for private in (
            storage_key,
            uuid.UUID(storage_key).hex,
            result.digest,
            PRIVATE_ROOT,
            str(self.cache_root),
        ):
            self.assertNotIn(private, serialized)
        self.assertIn('deploy.py', serialized)


class PublicationTestCase(ValidationTestMixin, TestCase):
    def test_a_valid_verdict_records_what_the_revision_publishes(self):
        self.declare('deploy.py')
        result = validate_revision(self.stage(SCRIPT_FILES), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        (record,) = result.discovered_scripts
        self.assertEqual(record['module_path'], 'deploy')
        self.assertEqual(record['class_name'], 'Deploy')
        self.assertEqual(record['entrypoint_path'], 'deploy.py')
        self.assertEqual(record['position'], 0)
        self.assertEqual(record['metadata']['commit_default'], True)

    def test_the_recorded_entrypoint_is_the_declaration_that_published_the_class(self):
        module_row = self.declare('deploy.py')
        result = validate_revision(self.stage(SCRIPT_FILES), job=self.job)
        (record,) = result.discovered_scripts
        self.assertEqual(record['entrypoint_module_id'], module_row.pk)

    def test_a_helper_defined_class_records_its_defining_module(self):
        self.declare('deploy.py')
        files = {
            'helpers.py': script_source('Shared'),
            'deploy.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
        }
        result = validate_revision(self.stage(files), job=self.job)
        (record,) = result.discovered_scripts
        # No Module row names helpers.py, which is why a published class cannot be identified
        # by its entrypoint declaration.
        self.assertEqual(record['module_path'], 'helpers')
        self.assertEqual(record['entrypoint_path'], 'deploy.py')

    def test_positions_number_the_publication_set_across_entries(self):
        self.declare('deploy.py')
        self.declare('audit.py')
        files = {'deploy.py': script_source('Deploy'), 'audit.py': script_source('Audit')}
        result = validate_revision(self.stage(files), job=self.job)
        self.assertEqual([record['position'] for record in result.discovered_scripts], [0, 1])
        self.assertEqual(len({record['class_name'] for record in result.discovered_scripts}), 2)

    def test_one_class_reexported_by_two_entries_is_recorded_once(self):
        self.declare('first.py')
        self.declare('second.py')
        files = {
            'helpers.py': script_source('Shared'),
            'first.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
            'second.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
        }
        result = validate_revision(self.stage(files), job=self.job)
        self.assertEqual(len(result.discovered_scripts), 1)
        self.assertEqual(result.discovered_scripts[0]['position'], 0)

    def test_an_empty_snapshot_publishes_nothing(self):
        result = validate_revision(self.stage(SCRIPT_FILES), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.discovered_scripts, [])

    def test_a_run_form_fault_is_an_invalid_verdict_that_publishes_nothing(self):
        self.declare('deploy.py')
        source = (
            b'from netbox_custom_scripts.scripts import Script\n'
            b'from netbox_custom_scripts.scripts.variables import ScriptVariable\n\n\n'
            b'class Broken(ScriptVariable):\n'
            b'    def __init__(self):\n'
            b'        super().__init__()\n'
            b"        self.field_attrs['max_digits'] = 4\n\n\n"
            b'class Deploy(Script):\n'
            b'    alpha = Broken()\n'
        )
        result = validate_revision(self.stage({'deploy.py': source}), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual(result.discovered_scripts, [])
        (record,) = result.validation_errors
        self.assertEqual(record['code'], 'form_construction_failed')
        self.assertEqual(record['source_path'], 'deploy.py')

    def test_a_variable_shadowing_the_commit_toggle_is_an_invalid_verdict(self):
        self.declare('deploy.py')
        source = (
            b'from netbox_custom_scripts.scripts import Script\n'
            b'from netbox_custom_scripts.scripts.variables import StringVar\n\n\n'
            b'class Deploy(Script):\n'
            b'    _commit = StringVar()\n'
        )
        result = validate_revision(self.stage({'deploy.py': source}), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual(result.validation_errors[0]['code'], 'reserved_variable_name')

    def test_a_fieldset_naming_a_missing_variable_is_an_invalid_verdict(self):
        self.declare('deploy.py')
        source = (
            b'from netbox_custom_scripts.scripts import Script\n'
            b'from netbox_custom_scripts.scripts.variables import StringVar\n\n\n'
            b'class Deploy(Script):\n'
            b'    alpha = StringVar()\n\n'
            b'    class Meta:\n'
            b"        fieldsets = (('Data', ('alpha', 'missing')),)\n"
        )
        result = validate_revision(self.stage({'deploy.py': source}), job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual(result.validation_errors[0]['code'], 'unknown_fieldset_field')

    def test_one_form_fault_is_charged_only_to_its_own_entry(self):
        good = self.declare('deploy.py')
        bad = self.declare('broken.py')
        source = (
            b'from netbox_custom_scripts.scripts import Script\n'
            b'from netbox_custom_scripts.scripts.variables import StringVar\n\n\n'
            b'class Broken(Script):\n'
            b'    _commit = StringVar()\n'
        )
        files = {'deploy.py': script_source('Deploy'), 'broken.py': source}
        result = validate_revision(self.stage(files), job=self.job)
        # One verdict covers the whole revision, so nothing publishes, but the failure is
        # charged to the entry that carried it rather than to its sibling.
        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual(result.discovered_scripts, [])
        self.assertEqual({record['source_path'] for record in result.validation_errors}, {'broken.py'})
        good.refresh_from_db()
        bad.refresh_from_db()
        self.assertEqual(good.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(bad.discovery_status, ModuleDiscoveryStatusChoices.FAILED)

    def test_an_environment_failure_leaves_the_publication_set_alone(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'import package_that_is_not_installed_anywhere\n'})
        with self.assertRaises(EntrypointImportError):
            validate_revision(revision, job=self.job)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(revision.discovered_scripts, [])

    def test_a_run_that_lost_its_lease_publishes_nothing(self):
        self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        original = validation._finalize

        def steal_then_finalize(target, job, status, validation_errors, discovered_scripts):
            CustomScriptProjectRevision.objects.filter(pk=target.pk).update(validation_job=self.make_job())
            return original(target, job, status, validation_errors, discovered_scripts)

        with mock.patch.object(validation, '_finalize', steal_then_finalize):
            validate_revision(revision, job=self.job)
        revision.refresh_from_db()
        self.assertEqual(revision.discovered_scripts, [])
        self.assertEqual(revision.status, RevisionStatusChoices.VALIDATING)

    def test_the_publication_set_is_json_safe(self):
        self.declare('deploy.py')
        result = validate_revision(self.stage(SCRIPT_FILES), job=self.job)
        self.assertEqual(json.loads(json.dumps(result.discovered_scripts)), result.discovered_scripts)


class ZeroPublicationTestCase(ValidationTestMixin, TestCase):
    """A revision whose enabled entrypoints import cleanly and publish no script."""

    def test_a_revision_publishing_nothing_is_invalid_and_says_why(self):
        module_row = self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'VALUE = 1\n'})

        result = validate_revision(revision, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        (record,) = result.validation_errors
        self.assertEqual(record['source_path'], 'deploy.py')
        self.assertEqual(record['code'], 'no_scripts_published')
        self.assertEqual(record['message'], 'The module imports cleanly and defines no Custom Script.')
        self.assertIsNone(record['exception_type'])
        self.assertIsNone(record['traceback'])
        self.assertEqual(result.discovered_scripts, [])
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.NO_SCRIPTS)
        self.assertEqual(module_row.discovery_error, 'The module imports cleanly and defines no Custom Script.')

    def test_an_entrypoint_publishing_nothing_beside_a_working_one_stays_valid(self):
        working = self.declare('deploy.py')
        empty = self.declare('notes.py')
        revision = self.stage({'deploy.py': script_source('Deploy'), 'notes.py': b'VALUE = 1\n'})

        result = validate_revision(revision, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.validation_errors, [])
        self.assertEqual([record['class_name'] for record in result.discovered_scripts], ['Deploy'])
        working.refresh_from_db()
        empty.refresh_from_db()
        self.assertEqual(working.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)
        self.assertEqual(working.discovery_error, '')
        self.assertEqual(empty.discovery_status, ModuleDiscoveryStatusChoices.NO_SCRIPTS)
        self.assertEqual(empty.discovery_error, 'The module imports cleanly and defines no Custom Script.')

    def test_an_empty_module_beside_a_failing_one_keeps_its_own_message(self):
        # The only case where a failure message and a zero-publication note are both in play,
        # so each Module row has to receive its own.
        empty = self.declare('notes.py')
        broken = self.declare('broken.py')
        revision = self.stage({'notes.py': b'VALUE = 1\n', 'broken.py': b'def broken(:\n'})

        result = validate_revision(revision, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        self.assertEqual({record['code'] for record in result.validation_errors}, {'entrypoint_import_failed'})
        empty.refresh_from_db()
        broken.refresh_from_db()
        self.assertEqual(empty.discovery_status, ModuleDiscoveryStatusChoices.NO_SCRIPTS)
        self.assertEqual(empty.discovery_error, 'The module imports cleanly and defines no Custom Script.')
        self.assertEqual(broken.discovery_status, ModuleDiscoveryStatusChoices.FAILED)
        self.assertEqual(broken.discovery_error, 'invalid syntax (broken.py, line 1)')

    def test_a_host_based_class_is_named_in_the_verdict(self):
        module_row = self.declare('deploy.py')
        # The documented bypass: importlib reaches the host module rather than the compat
        # stand-in, so the class subclasses NetBox Community's base and publishes nothing.
        source = (
            b'import importlib\n\n'
            b"host = importlib.import_module('extras.scripts')\n\n\n"
            b'class NewIP(host.Script):\n'
            b'    pass\n'
        )
        revision = self.stage({'deploy.py': source})

        result = validate_revision(revision, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.INVALID)
        (record,) = result.validation_errors
        self.assertEqual(record['code'], 'no_scripts_published')
        self.assertIn('"NewIP" subclasses extras.scripts.Script', record['message'])
        self.assertIn('netbox_custom_scripts.scripts', record['message'])
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.NO_SCRIPTS)

    def test_a_snapshot_with_no_enabled_entrypoint_is_still_valid(self):
        # The boundary the ruling drew: a revision with nothing enabled publishes nothing by
        # definition, which is every fresh Data Source project. The declaration exists here and
        # is merely disabled, which is the case the snapshot suite's empty-snapshot test does
        # not cover, and both short-circuit before the new rule is reached.
        self.declare('deploy.py', enabled=False)
        revision = self.stage({'deploy.py': b'VALUE = 1\n'})

        result = validate_revision(revision, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        self.assertEqual(result.validation_errors, [])


class ClassificationTestCase(TestCase):
    PREFIX = revision_module_name(uuid.UUID('9f1c6d24-0b2a-4d3e-8f57-2c9a4b6e1d80'), 'a' * 64)

    def wrap(self, cause):
        detail = {'path': 'p.py', 'code': 'entrypoint_import_failed', 'message': 'm', 'exception_type': 'T'}
        if cause is None:
            return EntrypointImportError('m', detail)
        try:
            raise EntrypointImportError('m', detail) from cause
        except EntrypointImportError as error:
            return error

    def test_the_classification_table(self):
        cases = (
            (None, 'content'),
            (SyntaxError('bad'), 'content'),
            (ModuleNotFoundError('missing', name=f'{self.PREFIX}.helpers'), 'content'),
            (ModuleNotFoundError('missing', name='numpy_absent'), 'environment'),
            (ImportError('cannot import name', name=self.PREFIX), 'content'),
            (ImportError('plain'), 'content'),
            (StorageError('backend down'), 'environment'),
            (OSError('disk failure'), 'environment'),
            (RuntimeError('project code raised'), 'content'),
            (SystemExit(1), 'content'),
        )
        for cause, expected in cases:
            with self.subTest(cause=type(cause).__name__ if cause else 'None'):
                error = self.wrap(cause)
                self.assertEqual(classify_entrypoint_error(error, revision_prefix=self.PREFIX), expected)

    def test_a_missing_name_in_an_installed_module_is_content(self):
        # from dcim.models import Devices. The module is there, the name is not, so the author
        # gets an invalid revision naming the typo instead of a failed job with no verdict.
        error = self.wrap(ImportError("cannot import name 'Devices' from 'dcim.models'", name='dcim.models'))
        self.assertEqual(classify_entrypoint_error(error, revision_prefix=self.PREFIX), 'content')

    def test_an_absent_distribution_is_still_environment(self):
        error = self.wrap(ModuleNotFoundError("No module named 'netaddr'", name='netaddr'))
        self.assertEqual(classify_entrypoint_error(error, revision_prefix=self.PREFIX), 'environment')

    def test_a_refused_legacy_import_is_content(self):
        # The compat tier raises a plain ImportError once the host drops a legacy name, and it
        # carries no name the prefix logic could match. Content is what records a verdict.
        error = self.wrap(ImportError('"extras.scripts" is no longer part of NetBox.', name='extras.scripts'))
        self.assertEqual(classify_entrypoint_error(error, revision_prefix=self.PREFIX), 'content')

    def test_an_absolute_import_of_the_revisions_own_module_is_content(self):
        # "import helpers" instead of "from . import helpers" raises without the revision
        # prefix, so without the manifest it reads exactly like a missing distribution and the
        # revision reverts to materialized forever instead of ever reaching invalid.
        error = self.wrap(ModuleNotFoundError('missing', name='helpers'))
        self.assertEqual(
            classify_entrypoint_error(error, revision_prefix=self.PREFIX, revision_modules={'helpers'}),
            'content',
        )
        self.assertEqual(
            classify_entrypoint_error(error, revision_prefix=self.PREFIX, revision_modules={'other'}),
            'environment',
        )

    def test_a_submodule_of_the_revisions_own_package_is_content(self):
        error = self.wrap(ModuleNotFoundError('missing', name='pkg.absent'))
        self.assertEqual(
            classify_entrypoint_error(error, revision_prefix=self.PREFIX, revision_modules={'pkg'}),
            'content',
        )

    def test_top_level_module_names_come_from_the_manifest(self):
        manifest = [
            {'path': 'helpers.py', 'size': 1, 'sha256': 'a' * 64},
            {'path': 'pkg/__init__.py', 'size': 1, 'sha256': 'b' * 64},
            {'path': 'pkg/deep/mod.py', 'size': 1, 'sha256': 'c' * 64},
            {'path': 'notes.txt', 'size': 1, 'sha256': 'd' * 64},
        ]
        self.assertEqual(_top_level_module_names(manifest), {'helpers', 'pkg'})

    def test_the_sanitizer_strips_every_identity_form(self):
        storage_key = '9f1c6d24-0b2a-4d3e-8f57-2c9a4b6e1d80'
        digest = 'b' * 64
        sanitize = build_error_sanitizer(storage_key, digest)
        prefix = revision_module_name(storage_key, digest)
        text = f'{prefix}.tools.deploy failed in {storage_key} at {digest} under {PRIVATE_ROOT}'
        cleaned = sanitize(text)
        for private in (storage_key, uuid.UUID(storage_key).hex, digest, PRIVATE_ROOT):
            self.assertNotIn(private, cleaned)
        self.assertIn('tools.deploy', cleaned)
        self.assertIsNone(sanitize(None))


class ModulePersistenceTestCase(ValidationTestMixin, TestCase):
    def test_a_row_renamed_since_staging_is_skipped(self):
        # save() refuses a rename now that the identity fields are frozen, so the mismatch
        # is produced through QuerySet.update(), the one route that still reaches it. The
        # persistence guard has to hold on that route too.
        module_row = self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        CustomScriptModule.objects.filter(pk=module_row.pk).update(source_path='moved.py')
        module_row.refresh_from_db()
        result = validate_revision(revision, job=self.job)
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.PENDING)
        self.assertIsNone(module_row.last_discovered_revision)

    def test_an_older_run_finishing_late_cannot_overwrite_a_newer_outcome(self):
        module_row = self.declare('deploy.py')
        older = self.stage({'deploy.py': script_source('One')})
        newer = self.stage({'deploy.py': script_source('Two')})
        newer_job = self.make_job()
        real_discover = validation.discover_scripts
        state = {'interleaved': False}

        def interleave(module, **kwargs):
            if not state['interleaved']:
                state['interleaved'] = True
                # The older revision's run pauses here while the newer revision's run
                # starts later and finishes first, writing the module outcome.
                validate_revision(newer, job=newer_job)
            return real_discover(module, **kwargs)

        with mock.patch.object(validation, 'discover_scripts', side_effect=interleave):
            result = validate_revision(older, job=self.job)

        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        module_row.refresh_from_db()
        self.assertEqual(module_row.last_discovered_revision_id, newer.pk)

    def test_an_expired_lease_revalidation_refreshes_its_own_module_rows(self):
        module_row = self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        validate_revision(revision, job=self.job)
        module_row.refresh_from_db()
        self.assertEqual(module_row.last_discovered_revision_id, revision.pk)
        # Simulate the terminal write being lost to a crash: back to a stale claim.
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALIDATING,
            validation_started=timezone.now() - timedelta(seconds=2 * VALIDATION_LEASE_SECONDS),
        )
        revision.refresh_from_db()
        result = validate_revision(revision, job=self.make_job())
        self.assertEqual(result.status, RevisionStatusChoices.VALID)
        module_row.refresh_from_db()
        self.assertEqual(module_row.last_discovered_revision_id, revision.pk)
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)


class RevisionValidationJobTestCase(ValidationTestMixin, TestCase):
    def test_an_immediate_enqueue_validates_the_revision(self):
        module_row = self.declare('deploy.py')
        revision = self.stage(SCRIPT_FILES)
        job = RevisionValidationJob.enqueue_validation(revision, immediate=True)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        module_row.refresh_from_db()
        self.assertEqual(module_row.discovery_status, ModuleDiscoveryStatusChoices.DISCOVERED)

    def test_enqueue_persists_the_revision_pk_on_the_job_row(self):
        revision = self.stage(SCRIPT_FILES)
        with self.captureOnCommitCallbacks():
            job = RevisionValidationJob.enqueue_validation(revision)
        job.refresh_from_db()
        self.assertEqual(job.data, {'revision_pk': revision.pk})

    def test_unsafe_routing_fails_the_run_before_touching_the_revision(self):
        revision = self.stage(SCRIPT_FILES)
        runner = RevisionValidationJob(self.make_job())
        with (
            mock.patch.object(jobs.branching, 'unsafe_routing_reason', return_value='Routing is unsafe.'),
            self.assertRaises(JobFailed),
        ):
            runner.run(revision_pk=revision.pk, job_id='later')
        self.assertIn('Routing is unsafe.', str(runner.job.log_entries))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)

    def test_a_deleted_revision_completes_with_nothing_to_do(self):
        revision = self.stage(SCRIPT_FILES)
        missing_pk = revision.pk
        revision.delete()
        job = RevisionValidationJob.enqueue(immediate=True, revision_pk=missing_pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)

    def test_an_environment_failure_fails_the_job_and_releases_the_claim(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'import package_that_is_not_installed_anywhere\n'})
        job = RevisionValidationJob.enqueue(immediate=True, revision_pk=revision.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_FAILED)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.MATERIALIZED)

    def test_an_invalid_verdict_completes_the_job(self):
        self.declare('deploy.py')
        revision = self.stage({'deploy.py': b'def broken(:\n'})
        runner = RevisionValidationJob(self.make_job())
        runner.run(revision_pk=revision.pk, job_id='x')
        self.assertIn('invalid', str(runner.job.log_entries))
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)

    def test_the_job_timeout_stays_below_the_lease(self):
        self.assertLess(VALIDATION_JOB_TIMEOUT, VALIDATION_LEASE_SECONDS)
