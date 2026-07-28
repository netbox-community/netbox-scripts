import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.db.models import ProtectedError
from django.db.utils import ConnectionDoesNotExist
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import DataSource
from netbox_custom_scripts import constants
from netbox_custom_scripts.choices import ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage.manifest import compute_digest

DIGEST_A = 'a' * 64
DIGEST_B = 'b' * 64
MANIFEST_A = [{'path': 'hello.py', 'size': 3, 'sha256': 'c' * 64}]


class CustomScriptProjectTestCase(TestCase):
    def test_create_customscriptproject(self):
        instance = CustomScriptProject.objects.create(
            name='Sample Project 1',
            key='sample-project-1',
            description='Created by the test suite.',
        )
        self.assertIsNotNone(instance.pk)
        self.assertEqual(instance.name, 'Sample Project 1')
        self.assertEqual(instance.source_type, ProjectSourceTypeChoices.UPLOAD)
        self.assertTrue(instance.enabled)

    def test_str(self):
        instance = CustomScriptProject(name='Sample Project 2', key='sample-project-2')
        self.assertEqual(str(instance), 'Sample Project 2')

    def test_absolute_url(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 3', key='sample-project-3')
        url = instance.get_absolute_url()
        self.assertEqual(
            url,
            reverse('plugins:netbox_custom_scripts:customscriptproject', args=[instance.pk]),
        )

    def test_storage_key_autoassigned(self):
        instance1 = CustomScriptProject.objects.create(name='Sample Project 4', key='sample-project-4')
        instance2 = CustomScriptProject.objects.create(name='Sample Project 5', key='sample-project-5')
        self.assertIsNotNone(instance1.storage_key)
        self.assertIsNotNone(instance2.storage_key)
        self.assertNotEqual(instance1.storage_key, instance2.storage_key)

    def test_upload_project_rejects_data_source(self):
        data_source = DataSource.objects.create(
            name='Data Source 1',
            type='local',
            source_url='file:///tmp/data-source-1/',
        )
        instance = CustomScriptProject(
            name='Sample Project 6',
            key='sample-project-6',
            source_type=ProjectSourceTypeChoices.UPLOAD,
            data_source=data_source,
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_source', cm.exception.message_dict)

    def test_upload_project_rejects_data_path(self):
        # The edit form no longer offers data_path on uploads, so guard clean() here.
        instance = CustomScriptProject(
            name='Sample Project 16',
            key='sample-project-16',
            source_type=ProjectSourceTypeChoices.UPLOAD,
            data_path='automation/netbox',
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_source_project_requires_data_source(self):
        instance = CustomScriptProject(
            name='Sample Project 7',
            key='sample-project-7',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_source', cm.exception.message_dict)

    def test_data_path_normalization(self):
        data_source = DataSource.objects.create(
            name='Data Source 2',
            type='local',
            source_url='file:///tmp/data-source-2/',
        )
        for raw, expected in (
            ('automation/netbox', 'automation/netbox'),
            ('automation/netbox/', 'automation/netbox'),
            ('./automation//netbox', 'automation/netbox'),
            (' automation/netbox ', 'automation/netbox'),
        ):
            with self.subTest(raw=raw):
                instance = CustomScriptProject(
                    name='Sample Project 8',
                    key='sample-project-8',
                    source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                    data_source=data_source,
                    data_path=raw,
                )
                instance.full_clean()
                self.assertEqual(instance.data_path, expected)

    def test_data_path_rejects_unsafe_values(self):
        data_source = DataSource.objects.create(
            name='Data Source 3',
            type='local',
            source_url='file:///tmp/data-source-3/',
        )
        for bad in ('/absolute/path', '../up', 'a/../b', 'a\\b', 'a\x00b'):
            with self.subTest(bad=bad):
                instance = CustomScriptProject(
                    name='Sample Project 9',
                    key='sample-project-9',
                    source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                    data_source=data_source,
                    data_path=bad,
                )
                with self.assertRaises(ValidationError) as cm:
                    instance.full_clean()
                self.assertIn('data_path', cm.exception.message_dict)

    def test_data_source_project_requires_nonempty_data_path(self):
        data_source = DataSource.objects.create(
            name='Data Source 4',
            type='local',
            source_url='file:///tmp/data-source-4/',
        )
        instance = CustomScriptProject(
            name='Sample Project 10',
            key='sample-project-10',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='',
        )
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_key_and_source_type_immutable(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 11', key='sample-project-11')
        instance.key = 'sample-project-11-renamed'
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('key', cm.exception.message_dict)

        instance.refresh_from_db()
        instance.source_type = ProjectSourceTypeChoices.DATA_SOURCE
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('source_type', cm.exception.message_dict)

    def test_storage_key_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 12', key='sample-project-12')
        instance.storage_key = uuid.uuid4()
        with self.assertRaises(ValidationError):
            instance.save()

    def test_storage_key_immutability_accepts_an_equal_string_form(self):
        # The persisted row reads back as a uuid.UUID while a caller may assign the equal
        # string form. Equality is decided on the field's python type, not on repr.
        instance = CustomScriptProject.objects.create(name='Sample Project 17', key='sample-project-17')
        instance.storage_key = str(instance.storage_key)
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.key, 'sample-project-17')

    def test_key_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 14', key='sample-project-14')
        instance.key = 'sample-project-14-renamed'
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('key', cm.exception.message_dict)

    def test_source_type_immutable_on_save(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 15', key='sample-project-15')
        instance.source_type = ProjectSourceTypeChoices.DATA_SOURCE
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_type', cm.exception.message_dict)

    def test_immutability_guard_reads_the_alias_the_save_writes_to(self):
        # Comparing the new value against a row fetched from a different connection compares
        # it against a different database, so the guard follows the save rather than the
        # router. The alias below does not exist, so the guard's own query is what fails.
        instance = CustomScriptProject.objects.create(name='Sample Project 16', key='sample-project-16')
        table = CustomScriptProject._meta.db_table
        with (
            CaptureQueriesContext(connections[DEFAULT_DB_ALIAS]) as captured,
            self.assertRaises(ConnectionDoesNotExist),
        ):
            instance.save(using='schema_example')
        self.assertEqual([entry for entry in captured.captured_queries if table in entry['sql']], [])

    def test_db_constraint_blocks_orm_bypass(self):
        instance = CustomScriptProject.objects.create(name='Sample Project 13', key='sample-project-13')
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProject.objects.filter(pk=instance.pk).update(data_path='sneaky/path')

    def test_data_path_rejects_exact_duplicate_on_same_data_source(self):
        data_source = DataSource.objects.create(name='DS Q15a', type='local', source_url='file:///tmp/q15a/')
        CustomScriptProject.objects.create(
            name='Q15 First',
            key='q15-first',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProject.objects.create(
                name='Q15 Duplicate',
                key='q15-duplicate',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=data_source,
                data_path='automation/netbox',
            )

    def test_data_path_rejects_ancestor_overlap_on_same_data_source(self):
        data_source = DataSource.objects.create(name='DS Q15b', type='local', source_url='file:///tmp/q15b/')
        CustomScriptProject.objects.create(
            name='Q15 Parent',
            key='q15-parent',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        child = CustomScriptProject(
            name='Q15 Child',
            key='q15-child',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        with self.assertRaises(ValidationError) as cm:
            child.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_path_rejects_descendant_overlap_on_same_data_source(self):
        data_source = DataSource.objects.create(name='DS Q15c', type='local', source_url='file:///tmp/q15c/')
        CustomScriptProject.objects.create(
            name='Q15 Deep',
            key='q15-deep',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        ancestor = CustomScriptProject(
            name='Q15 Ancestor',
            key='q15-ancestor',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        with self.assertRaises(ValidationError) as cm:
            ancestor.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_path_allows_sibling_paths_on_same_data_source(self):
        data_source = DataSource.objects.create(name='DS Q15d', type='local', source_url='file:///tmp/q15d/')
        CustomScriptProject.objects.create(
            name='Q15 Netbox',
            key='q15-netbox',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        sibling = CustomScriptProject(
            name='Q15 Netbox Old',
            key='q15-netbox-old',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox-old',
        )
        sibling.full_clean()

    def test_data_path_allows_identical_path_on_different_data_source(self):
        data_source_one = DataSource.objects.create(name='DS Q15e1', type='local', source_url='file:///tmp/q15e1/')
        data_source_two = DataSource.objects.create(name='DS Q15e2', type='local', source_url='file:///tmp/q15e2/')
        CustomScriptProject.objects.create(
            name='Q15 On DS1',
            key='q15-on-ds1',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_one,
            data_path='automation/netbox',
        )
        other = CustomScriptProject(
            name='Q15 On DS2',
            key='q15-on-ds2',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_two,
            data_path='automation/netbox',
        )
        other.full_clean()
        other.save()

    def test_upload_projects_unaffected_by_data_path_overlap_check(self):
        first = CustomScriptProject.objects.create(name='Q15 Upload A', key='q15-upload-a')
        second = CustomScriptProject.objects.create(name='Q15 Upload B', key='q15-upload-b')
        first.full_clean()
        second.full_clean()

    def test_active_revision_must_belong_to_project(self):
        owner = CustomScriptProject.objects.create(name='AR Owner', key='ar-owner')
        other = CustomScriptProject.objects.create(name='AR Other', key='ar-other')
        revision = CustomScriptProjectRevision.objects.create(project=owner, digest='e' * 64)
        other.active_revision = revision
        with self.assertRaises(ValidationError) as cm:
            other.full_clean()
        self.assertIn('active_revision', cm.exception.message_dict)

    def test_active_revision_accepts_an_active_revision(self):
        project = CustomScriptProject.objects.create(name='AR Own', key='ar-own')
        project.active_revision = CustomScriptProjectRevision.objects.create(
            project=project, digest='f' * 64, status=RevisionStatusChoices.ACTIVE
        )
        project.full_clean()
        project.save()
        project.refresh_from_db()
        self.assertIsNotNone(project.active_revision_id)

    def test_active_revision_rejects_a_revision_that_is_not_active(self):
        # The pointer means "this is being served", so a revision that was merely stored or
        # that validation rejected cannot occupy it.
        project = CustomScriptProject.objects.create(name='AR Status', key='ar-status')
        for digest, status in (
            ('a' * 64, RevisionStatusChoices.STAGING),
            ('b' * 64, RevisionStatusChoices.MATERIALIZED),
            ('c' * 64, RevisionStatusChoices.VALID),
            ('d' * 64, RevisionStatusChoices.INVALID),
        ):
            with self.subTest(status=status):
                project.active_revision = CustomScriptProjectRevision.objects.create(
                    project=project, digest=digest, status=status
                )
                with self.assertRaises(ValidationError) as cm:
                    project.full_clean()
                self.assertIn('active_revision', cm.exception.message_dict)

    def test_active_revision_for_reverse_accessor(self):
        project = CustomScriptProject.objects.create(name='AR Reverse', key='ar-reverse')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest='1' * 64)
        self.assertFalse(revision.active_revision_for.exists())
        project.active_revision = revision
        project.save()
        self.assertEqual(list(revision.active_revision_for.all()), [project])

    def test_delete_project_with_active_revision_succeeds(self):
        project = CustomScriptProject.objects.create(name='AR Delete', key='ar-delete')
        # The deletion signal validates a captured manifest against its digest, so a
        # deletable fixture must carry a pair that actually matches.
        digest = compute_digest([])
        revision = CustomScriptProjectRevision.objects.create(project=project, digest=digest)
        project.active_revision = revision
        project.save()
        project.delete()
        self.assertFalse(CustomScriptProject.objects.filter(key='ar-delete').exists())
        self.assertFalse(CustomScriptProjectRevision.objects.filter(digest=digest).exists())

    def test_delete_active_revision_directly_raises_protected_error(self):
        project = CustomScriptProject.objects.create(name='AR Protect', key='ar-protect')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest='3' * 64)
        project.active_revision = revision
        project.save()
        with self.assertRaises(ProtectedError):
            revision.delete()

    def test_bulk_queryset_delete_bypasses_active_revision_clearing(self):
        # Documents the caveat: QuerySet.delete() never calls the model's delete().
        project = CustomScriptProject.objects.create(name='AR Bulk', key='ar-bulk')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest='4' * 64)
        project.active_revision = revision
        project.save()
        with self.assertRaises(ProtectedError), transaction.atomic():
            CustomScriptProject.objects.filter(pk=project.pk).delete()


class CustomScriptProjectRevisionTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Revision Project', key='revision-project')

    def make_revision(self, **overrides):
        values = {
            'project': self.project,
            'digest': DIGEST_A,
            'status': RevisionStatusChoices.VALID,
            'manifest': MANIFEST_A,
            'file_count': 1,
            'total_size': 3,
        }
        values.update(overrides)
        return CustomScriptProjectRevision.objects.create(**values)

    def test_create_revision(self):
        instance = self.make_revision()
        self.assertIsNotNone(instance.pk)
        self.assertEqual(instance.project, self.project)
        self.assertEqual(instance.digest, DIGEST_A)
        self.assertEqual(instance.manifest, MANIFEST_A)
        self.assertEqual(instance.validation_errors, [])
        self.assertIsNone(instance.activated)
        self.assertIsNotNone(instance.created)

    def test_default_status_is_staging(self):
        instance = CustomScriptProjectRevision.objects.create(project=self.project, digest=DIGEST_A)
        self.assertEqual(instance.status, RevisionStatusChoices.STAGING)

    def test_revisions_are_reachable_from_the_project(self):
        instance = self.make_revision()
        self.assertEqual(list(self.project.revisions.all()), [instance])

    def test_str(self):
        instance = self.make_revision()
        self.assertEqual(str(instance), f'{self.project} @ {DIGEST_A[:12]}')

    def test_str_tolerates_null_digest(self):
        instance = self.make_revision(digest=None, status=RevisionStatusChoices.INVALID)
        self.assertEqual(str(instance), f'{self.project} @ invalid')

    def test_digest_field_validates_hex_format(self):
        instance = CustomScriptProjectRevision(project=self.project, digest='NOT-A-DIGEST')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('digest', cm.exception.message_dict)

    def test_digest_field_rejects_uppercase_hex(self):
        instance = CustomScriptProjectRevision(project=self.project, digest='A' * 64)
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('digest', cm.exception.message_dict)

    def test_unique_project_digest_constraint(self):
        self.make_revision()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_revision()

    def test_same_digest_allowed_on_a_different_project(self):
        other = CustomScriptProject.objects.create(name='Other Project', key='other-project')
        self.make_revision()
        instance = CustomScriptProjectRevision.objects.create(project=other, digest=DIGEST_A)
        self.assertIsNotNone(instance.pk)

    def test_multiple_invalid_revisions_allowed_with_null_digest(self):
        first = self.make_revision(digest=None, status=RevisionStatusChoices.INVALID)
        second = self.make_revision(digest=None, status=RevisionStatusChoices.INVALID)
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(CustomScriptProjectRevision.objects.filter(digest__isnull=True).count(), 2)

    def test_every_status_but_invalid_requires_a_digest(self):
        # A digest is computed from accepted content before the row is created, so a status
        # other than invalid always follows content that has an address, even when a caller
        # bypasses the storage service. Only source rejection has nothing to point at.
        requiring = [status for status in RevisionStatusChoices.values() if status != RevisionStatusChoices.INVALID]
        self.assertEqual(len(requiring), 7)
        for status in requiring:
            with self.subTest(status=status), self.assertRaises(IntegrityError), transaction.atomic():
                self.make_revision(digest=None, status=status)

    def test_invalid_status_may_lack_a_digest_or_carry_one(self):
        # Source rejection has no content address. Semantic rejection of stored content does.
        rejected = self.make_revision(digest=None, status=RevisionStatusChoices.INVALID)
        semantic = self.make_revision(digest=DIGEST_B, status=RevisionStatusChoices.INVALID)
        self.assertIsNone(rejected.digest)
        self.assertEqual(semantic.digest, DIGEST_B)

    def test_only_one_active_revision_per_project(self):
        self.make_revision(digest=DIGEST_A, status=RevisionStatusChoices.ACTIVE)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_revision(digest=DIGEST_B, status=RevisionStatusChoices.ACTIVE)

    def test_two_projects_may_each_have_an_active_revision(self):
        other = CustomScriptProject.objects.create(name='Other Active', key='other-active')
        self.make_revision(digest=DIGEST_A, status=RevisionStatusChoices.ACTIVE)
        sibling = CustomScriptProjectRevision.objects.create(
            project=other, digest=DIGEST_A, status=RevisionStatusChoices.ACTIVE
        )
        self.assertIsNotNone(sibling.pk)

    def test_project_field_immutable_on_save(self):
        instance = self.make_revision()
        instance.project = CustomScriptProject.objects.create(name='Moved To', key='moved-to')
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('project', cm.exception.message_dict)

    def test_digest_field_immutable_on_save(self):
        instance = self.make_revision()
        instance.digest = DIGEST_B
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('digest', cm.exception.message_dict)

    def test_manifest_field_immutable_on_save(self):
        instance = self.make_revision()
        instance.manifest = [{'path': 'other.py', 'size': 1, 'sha256': 'd' * 64}]
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('manifest', cm.exception.message_dict)

    def test_file_count_and_total_size_immutable_on_save(self):
        instance = self.make_revision()
        instance.file_count = 99
        instance.total_size = 12345
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('file_count', cm.exception.message_dict)
        self.assertIn('total_size', cm.exception.message_dict)

    def test_status_and_validation_errors_and_activated_remain_mutable(self):
        instance = self.make_revision()
        moment = timezone.now()
        instance.status = RevisionStatusChoices.ACTIVE
        instance.validation_errors = [{'path': None, 'code': 'storage_write_failed', 'message': 'disk full'}]
        instance.activated = moment
        instance.save()

        instance.refresh_from_db()
        self.assertEqual(instance.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(instance.validation_errors[0]['code'], 'storage_write_failed')
        self.assertEqual(instance.activated, moment)

    def test_deleting_the_project_cascades_to_its_revisions(self):
        project = CustomScriptProject.objects.create(name='Doomed Project', key='doomed-project')
        digest = compute_digest([])
        CustomScriptProjectRevision.objects.create(project=project, digest=digest)
        project.delete()
        self.assertFalse(CustomScriptProjectRevision.objects.filter(digest=digest).exists())

    def test_ordering_is_newest_first(self):
        older = self.make_revision(digest=DIGEST_A)
        newer = self.make_revision(digest=DIGEST_B)
        CustomScriptProjectRevision.objects.filter(pk=older.pk).update(created=timezone.now() - timedelta(days=1))
        self.assertEqual(list(CustomScriptProjectRevision.objects.all()), [newer, older])

    def test_lifecycle_groupings_match_the_choice_set(self):
        # The groupings are spelled out in constants.py so that module keeps no imports, so
        # this is the guard that stops the two from drifting apart.
        valid_values = set(RevisionStatusChoices.values())
        groups = (
            constants.ACTIVATABLE_REVISION_STATUSES,
            constants.STORED_REVISION_STATUSES,
            constants.RETRYABLE_REVISION_STATUSES,
        )
        for group in groups:
            with self.subTest(group=group):
                self.assertEqual(set(group) - valid_values, set())
                self.assertEqual(len(set(group)), len(group))

    def test_lifecycle_groupings_are_internally_consistent(self):
        activatable = set(constants.ACTIVATABLE_REVISION_STATUSES)
        stored = set(constants.STORED_REVISION_STATUSES)
        retryable = set(constants.RETRYABLE_REVISION_STATUSES)
        # A revision cannot be both safe to re-drive and ready to serve.
        self.assertEqual(activatable & retryable, set())
        # Anything servable must already be on disk.
        self.assertLessEqual(activatable, stored)
        # A rejected revision is terminal for the storage layer, which is what stops a
        # re-stage from clearing a validation verdict.
        self.assertNotIn(RevisionStatusChoices.INVALID, stored | retryable)
        self.assertNotIn(RevisionStatusChoices.MATERIALIZED, activatable)
        # Project validation owns VALIDATING, so the storage layer must never re-drive it.
        # Without this the validator's own transition would be reversible by a re-stage.
        self.assertNotIn(RevisionStatusChoices.VALIDATING, stored | retryable)
