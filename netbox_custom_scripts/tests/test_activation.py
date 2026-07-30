import uuid
from unittest import mock

from django.db import DEFAULT_DB_ALIAS, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from core.models import Job, ObjectType
from netbox_custom_scripts import activation
from netbox_custom_scripts.activation import activate_revision, synchronize_scripts
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import (
    CustomScript,
    CustomScriptProject,
    CustomScriptProjectRevision,
)
from netbox_custom_scripts.runtime import loader
from netbox_custom_scripts.storage import service, store
from netbox_custom_scripts.storage.exceptions import ActivationError
from netbox_custom_scripts.tests.storage.test_service import StorageServiceMixin, content

DIGEST_A = 'a' * 64
DIGEST_B = 'b' * 64
SCRIPT_TABLE = CustomScript._meta.db_table


def record(module_path='deploy', class_name='DeployDevices', position=0, **overrides):
    """Build one snapshot record the way runtime introspection would."""
    values = {
        'module_path': module_path,
        'class_name': class_name,
        'entrypoint_module_id': 12,
        'entrypoint_path': 'deploy.py',
        'position': position,
        'display_name': 'Deploy Devices',
        'description': 'Deploy devices at a site.',
        'metadata': {
            'commit_default': True,
            'scheduling_enabled': True,
            'job_timeout': None,
            'notifications_default': 'always',
        },
    }
    values.update(overrides)
    return values


class SynchronizeScriptsTestCase(TestCase):
    def setUp(self):
        self.project = CustomScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.revision = CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def sync(self, records, revision=None, project=None):
        """Synchronize one project against a snapshot, the way the promotion callback does."""
        synchronize_scripts(
            project=project or self.project,
            revision=revision or self.revision,
            records=records,
            using=DEFAULT_DB_ALIAS,
        )

    def test_a_new_identity_creates_an_enabled_row(self):
        self.sync([record()])
        row = CustomScript.objects.get()
        self.assertEqual(row.module_path, 'deploy')
        self.assertEqual(row.class_name, 'DeployDevices')
        self.assertEqual(row.display_name, 'Deploy Devices')
        self.assertEqual(row.last_seen_revision, self.revision)
        self.assertTrue(row.enabled)
        self.assertFalse(row.is_retired)

    def test_an_existing_row_refreshes_every_recorded_field(self):
        self.sync([record()])
        later = CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_B,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        self.sync(
            [
                record(
                    display_name='Deploy Devices Everywhere',
                    description='Deploy devices at every site.',
                    metadata={'commit_default': False},
                )
            ],
            revision=later,
        )
        row = CustomScript.objects.get()
        self.assertEqual(row.display_name, 'Deploy Devices Everywhere')
        self.assertEqual(row.description, 'Deploy devices at every site.')
        self.assertEqual(row.metadata, {'commit_default': False})
        self.assertEqual(row.last_seen_revision, later)

    def test_a_missing_identity_retires_its_row_rather_than_deleting_it(self):
        self.sync([record()])
        self.sync([])
        self.assertEqual(CustomScript.objects.count(), 1)
        self.assertTrue(CustomScript.objects.get().is_retired)

    def test_a_retired_row_keeps_the_revision_that_last_published_it(self):
        self.sync([record()])
        self.sync([])
        self.assertEqual(CustomScript.objects.get().last_seen_revision, self.revision)

    def test_a_reappearing_identity_reuses_the_same_row(self):
        self.sync([record()])
        original_pk = CustomScript.objects.get().pk
        self.sync([])
        self.sync([record()])
        row = CustomScript.objects.get()
        self.assertEqual(row.pk, original_pk)
        self.assertFalse(row.is_retired)

    def test_synchronization_never_touches_enabled(self):
        # enabled is the administrator's field. A disabled script stays disabled through a
        # refresh, a retirement, and a return.
        self.sync([record()])
        CustomScript.objects.update(enabled=False)
        self.sync([record(display_name='Renamed')])
        self.assertFalse(CustomScript.objects.get().enabled)
        self.sync([])
        self.assertFalse(CustomScript.objects.get().enabled)
        self.sync([record()])
        self.assertFalse(CustomScript.objects.get().enabled)

    def test_a_moved_class_becomes_a_new_identity_and_retires_the_old_one(self):
        self.sync([record()])
        self.sync([record(module_path='helpers')])
        retired = CustomScript.objects.get(module_path='deploy')
        current = CustomScript.objects.get(module_path='helpers')
        self.assertTrue(retired.is_retired)
        self.assertFalse(current.is_retired)

    def test_a_renamed_class_becomes_a_new_identity_and_retires_the_old_one(self):
        self.sync([record()])
        self.sync([record(class_name='DeployEverything')])
        self.assertTrue(CustomScript.objects.get(class_name='DeployDevices').is_retired)
        self.assertFalse(CustomScript.objects.get(class_name='DeployEverything').is_retired)

    def test_a_description_longer_than_the_inherited_field_round_trips(self):
        # The model overrides the abstract base CharField with a TextField, so nothing
        # truncates an unbounded Meta.description.
        description = 'd' * 500
        self.sync([record(description=description)])
        self.assertEqual(CustomScript.objects.get().description, description)

    def test_another_project_is_left_alone(self):
        other = CustomScriptProject.objects.create(name='Audit', key='audit')
        other_revision = CustomScriptProjectRevision.objects.create(
            project=other,
            digest=DIGEST_B,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        self.sync([record()])
        self.sync([record()], revision=other_revision, project=other)
        self.sync([], revision=other_revision, project=other)
        self.assertFalse(CustomScript.objects.get(project=self.project).is_retired)
        self.assertTrue(CustomScript.objects.get(project=other).is_retired)

    def test_an_unchanged_snapshot_issues_no_write(self):
        # Load bearing rather than an optimization. This runs again on every activation, so an
        # unconditional save would log a change and queue an event per script per activation.
        self.sync([record()])
        with CaptureQueriesContext(connection) as captured:
            self.sync([record()])
        writes = [
            query['sql']
            for query in captured.captured_queries
            if f'INSERT INTO "{SCRIPT_TABLE}"' in query['sql'] or f'UPDATE "{SCRIPT_TABLE}"' in query['sql']
        ]
        self.assertEqual(writes, [])

    def test_a_partial_change_writes_only_the_fields_that_differ(self):
        self.sync([record()])
        with CaptureQueriesContext(connection) as captured:
            self.sync([record(display_name='Renamed')])
        (update,) = [q['sql'] for q in captured.captured_queries if f'UPDATE "{SCRIPT_TABLE}"' in q['sql']]
        self.assertIn('display_name', update)
        self.assertNotIn('description', update)

    def test_job_history_survives_a_retirement_and_a_return(self):
        # Retirement preserves the primary key, and JobsMixin resolves history by object id
        # with no database constraint behind it.
        self.sync([record()])
        row = CustomScript.objects.get()
        Job.objects.create(
            name='deploy-devices',
            job_id=uuid.uuid4(),
            # for_concrete_model=False matches the GenericRelation JobsMixin declares.
            object_type=ObjectType.objects.get_for_model(row, for_concrete_model=False),
            object_id=row.pk,
        )
        self.sync([])
        self.sync([record()])
        self.assertEqual(CustomScript.objects.get().jobs.count(), 1)


class ActivationMixin(StorageServiceMixin):
    """A project with real stored content, plus a way to give a revision recorded scripts."""

    def published(self, revision, records):
        """Record a discovery snapshot the only way anything reaches that field."""
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(discovered_scripts=records)
        revision.refresh_from_db()
        return revision

    def valid_with(self, records, marker=1):
        """Stage distinct content, promote it to VALID, and record one snapshot on it."""
        return self.published(self.validated(content(marker)), records)


class ActivateRevisionTestCase(ActivationMixin, TestCase):
    def test_activation_publishes_a_row_for_every_recorded_script(self):
        revision = self.valid_with([record(), record(class_name='AuditDevices', position=1)])
        activate_revision(revision)
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertEqual(
            sorted(CustomScript.objects.values_list('class_name', flat=True)),
            ['AuditDevices', 'DeployDevices'],
        )

    def test_a_later_revision_retires_what_it_stops_publishing(self):
        activate_revision(self.valid_with([record(), record(class_name='AuditDevices', position=1)], marker=1))
        activate_revision(self.valid_with([record(class_name='AuditDevices')], marker=2))
        self.assertTrue(CustomScript.objects.get(class_name='DeployDevices').is_retired)
        self.assertFalse(CustomScript.objects.get(class_name='AuditDevices').is_retired)

    def test_reactivating_the_active_revision_repairs_a_lost_row(self):
        # The callback runs before the already-active early return, so activation is the repair.
        revision = self.valid_with([record()])
        activate_revision(revision)
        CustomScript.objects.all().delete()
        activate_revision(revision)
        self.assertEqual(CustomScript.objects.get().class_name, 'DeployDevices')

    def test_activation_imports_nothing(self):
        # Re-importing could disagree with the verdict the revision already carries.
        revision = self.valid_with([record()])
        with mock.patch.object(loader, 'import_entrypoint', side_effect=AssertionError('imported')):
            activate_revision(revision)
        self.project.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, revision.pk)

    def test_a_damaged_snapshot_is_refused_before_the_project_lock(self):
        # Failing fast means a damaged snapshot never pays for a full tree verification.
        revision = self.valid_with([record(position=7)])
        with (
            mock.patch.object(store, 'verify_revision_tree') as verify,
            self.assertRaises(ActivationError) as captured,
        ):
            activate_revision(revision)
        self.assertIn('unusable', str(captured.exception))
        verify.assert_not_called()
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)

    def test_a_snapshot_damaged_only_on_the_locked_row_is_refused(self):
        # The pre-lock check passed, so only the check inside the callback can catch this.
        revision = self.valid_with([record()])
        real_verify = store.verify_revision_tree

        def verify_then_damage(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(discovered_scripts=[record(position=7)])
            return result

        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=verify_then_damage),
            self.assertRaises(ActivationError),
        ):
            activate_revision(revision)
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)
        self.assertFalse(CustomScript.objects.exists())

    def test_a_snapshot_swapped_after_its_return_trip_check_is_refused(self):
        # Covers the widened post-lock comparison. The swap is well formed, so only comparing
        # the locked row against the verified snapshot can catch it.
        revision = self.valid_with([record()])
        real_verify = store.verify_revision_tree

        def verify_then_swap(*args, **kwargs):
            result = real_verify(*args, **kwargs)
            CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
                discovered_scripts=[record(class_name='Swapped')]
            )
            return result

        with (
            mock.patch.object(store, 'verify_revision_tree', side_effect=verify_then_swap),
            self.assertRaises(ActivationError) as captured,
        ):
            activate_revision(revision)
        self.assertIn('changed while', str(captured.exception))
        self.project.refresh_from_db()
        self.assertIsNone(self.project.active_revision_id)
        self.assertFalse(CustomScript.objects.exists())


class PromotionCallbackTestCase(ActivationMixin, TestCase):
    def test_the_callback_is_required(self):
        # A default would leave the bypass one call away from the invariant.
        with self.assertRaises(TypeError):
            service.promote_revision(self.validated())

    def test_the_callback_runs_inside_a_transaction_the_primitive_opened(self):
        # Asserting in_atomic_block would prove nothing: TestCase already wraps every test body
        # in one. The savepoint stack getting deeper is what shows the primitive opened its own.
        revision = self.validated()
        outer = len(connection.savepoint_ids)
        seen = []
        service.promote_revision(revision, on_promote=lambda **kwargs: seen.append(len(connection.savepoint_ids)))
        self.assertEqual(len(seen), 1)
        self.assertGreater(seen[0], outer)

    def test_the_callback_runs_on_the_already_active_path(self):
        revision = self.validated()
        calls = []
        service.promote_revision(revision, on_promote=lambda **kwargs: calls.append(kwargs))
        service.promote_revision(revision, on_promote=lambda **kwargs: calls.append(kwargs))
        self.assertEqual(len(calls), 2)

    def test_the_callback_receives_the_locked_rows_and_the_alias(self):
        revision = self.validated()
        seen = {}
        service.promote_revision(revision, on_promote=lambda **kwargs: seen.update(kwargs))
        self.assertEqual(seen['project'].pk, self.project.pk)
        self.assertEqual(seen['revision'].pk, revision.pk)
        self.assertEqual(seen['using'], DEFAULT_DB_ALIAS)

    def test_a_failing_callback_rolls_the_whole_promotion_back(self):
        first = self.valid_with([record()], marker=1)
        activate_revision(first)
        second = self.valid_with([record(class_name='Later')], marker=2)
        with (
            mock.patch.object(activation, 'synchronize_scripts', side_effect=RuntimeError('boom')),
            self.assertRaises(RuntimeError),
        ):
            activate_revision(second)
        self.project.refresh_from_db()
        second.refresh_from_db()
        first.refresh_from_db()
        self.assertEqual(self.project.active_revision_id, first.pk)
        self.assertEqual(first.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(second.status, RevisionStatusChoices.VALID)
        self.assertFalse(CustomScript.objects.filter(class_name='Later').exists())
