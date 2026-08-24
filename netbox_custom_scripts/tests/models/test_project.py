import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.db.models.deletion import Collector
from django.db.utils import ConnectionDoesNotExist
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from core.models import DataSource
from netbox_custom_scripts import constants
from netbox_custom_scripts.choices import ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage.entrypoints import EMPTY_SNAPSHOT_DIGEST
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

    def test_data_source_project_refuses_the_data_source_root(self):
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
        data_source = DataSource.objects.create(
            name='Duplicate Path Source', type='local', source_url='file:///tmp/duplicate-path/'
        )
        CustomScriptProject.objects.create(
            name='Automation Netbox',
            key='automation-netbox',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProject.objects.create(
                name='Automation Netbox Again',
                key='automation-netbox-again',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=data_source,
                data_path='automation/netbox',
            )

    def test_data_path_rejects_ancestor_overlap_on_same_data_source(self):
        data_source = DataSource.objects.create(
            name='Ancestor Overlap Source', type='local', source_url='file:///tmp/ancestor-overlap/'
        )
        CustomScriptProject.objects.create(
            name='Automation Tree',
            key='automation-tree',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        child = CustomScriptProject(
            name='Automation Subtree',
            key='automation-subtree',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        with self.assertRaises(ValidationError) as cm:
            child.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_path_rejects_descendant_overlap_on_same_data_source(self):
        data_source = DataSource.objects.create(
            name='Descendant Overlap Source', type='local', source_url='file:///tmp/descendant-overlap/'
        )
        CustomScriptProject.objects.create(
            name='Deep Path',
            key='deep-path',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        ancestor = CustomScriptProject(
            name='Enclosing Path',
            key='enclosing-path',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        with self.assertRaises(ValidationError) as cm:
            ancestor.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)

    def test_data_path_allows_sibling_paths_on_same_data_source(self):
        data_source = DataSource.objects.create(
            name='Sibling Path Source', type='local', source_url='file:///tmp/sibling-paths/'
        )
        CustomScriptProject.objects.create(
            name='Netbox Scripts',
            key='netbox-scripts',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        sibling = CustomScriptProject(
            name='Netbox Scripts Old',
            key='netbox-scripts-old',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox-old',
        )
        sibling.full_clean()

    def test_data_path_allows_identical_path_on_different_data_source(self):
        data_source_one = DataSource.objects.create(
            name='First Shared Path Source', type='local', source_url='file:///tmp/shared-path-one/'
        )
        data_source_two = DataSource.objects.create(
            name='Second Shared Path Source', type='local', source_url='file:///tmp/shared-path-two/'
        )
        CustomScriptProject.objects.create(
            name='Same Path First Source',
            key='same-path-first-source',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_one,
            data_path='automation/netbox',
        )
        other = CustomScriptProject(
            name='Same Path Second Source',
            key='same-path-second-source',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_two,
            data_path='automation/netbox',
        )
        other.full_clean()
        other.save()

    def test_an_ancestor_project_overlaps_a_subdirectory_project(self):
        data_source = DataSource.objects.create(
            name='Root Overlap Source', type='local', source_url='file:///tmp/root-overlap/'
        )
        CustomScriptProject.objects.create(
            name='Scripts Directory',
            key='scripts-directory',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/scripts',
        )
        ancestor = CustomScriptProject(
            name='Automation Directory',
            key='automation-directory',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        with self.assertRaises(ValidationError) as cm:
            ancestor.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)
        # Named, so this cannot pass on some other data_path error.
        self.assertIn('Scripts Directory', str(cm.exception.message_dict['data_path']))

    def test_a_subdirectory_project_overlaps_an_existing_ancestor_project(self):
        data_source = DataSource.objects.create(
            name='Root First Source', type='local', source_url='file:///tmp/root-first/'
        )
        CustomScriptProject.objects.create(
            name='Whole Repository',
            key='whole-repository',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation',
        )
        child = CustomScriptProject(
            name='Scripts Subdirectory',
            key='scripts-subdirectory',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/scripts',
        )
        with self.assertRaises(ValidationError) as cm:
            child.full_clean()
        self.assertIn('data_path', cm.exception.message_dict)
        self.assertIn('Whole Repository', str(cm.exception.message_dict['data_path']))

    def test_the_same_path_is_allowed_on_another_data_source(self):
        data_source_one = DataSource.objects.create(
            name='First Root Source', type='local', source_url='file:///tmp/root-source-one/'
        )
        data_source_two = DataSource.objects.create(
            name='Second Root Source', type='local', source_url='file:///tmp/root-source-two/'
        )
        CustomScriptProject.objects.create(
            name='Root On First Source',
            key='root-on-first-source',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_one,
            data_path='automation',
        )
        other = CustomScriptProject(
            name='Root On Second Source',
            key='root-on-second-source',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source_two,
            data_path='automation',
        )
        other.full_clean()
        other.save()

    def test_the_database_constraint_refuses_a_root_data_source_project(self):
        """objects.create() skips clean(), so this reaches enforce_source_ownership directly."""
        data_source = DataSource.objects.create(
            name='Constraint Check Source', type='local', source_url='file:///tmp/constraint-check/'
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomScriptProject.objects.create(
                name='Unvalidated Root',
                key='unvalidated-root',
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=data_source,
                data_path='',
            )

    def test_upload_projects_unaffected_by_data_path_overlap_check(self):
        first = CustomScriptProject.objects.create(name='Overlap Exempt Upload A', key='overlap-exempt-upload-a')
        second = CustomScriptProject.objects.create(name='Overlap Exempt Upload B', key='overlap-exempt-upload-b')
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

    def test_deleting_the_active_revision_clears_the_pointer(self):
        # SET_NULL rather than PROTECT. A project that loses its active revision serves nothing
        # until another is activated, which is the same state it starts life in.
        project = CustomScriptProject.objects.create(name='AR Clear', key='ar-clear')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest=compute_digest([]))
        project.active_revision = revision
        project.save()
        revision.delete()
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_a_queryset_delete_removes_a_project_with_an_active_revision(self):
        # The regression test for the pointer that protected its own project. PROTECT fired
        # here even though the protecting row was the project being deleted.
        project = CustomScriptProject.objects.create(name='AR Bulk', key='ar-bulk')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest=compute_digest([]))
        project.active_revision = revision
        project.save()
        CustomScriptProject.objects.filter(pk=project.pk).delete()
        self.assertFalse(CustomScriptProject.objects.filter(key='ar-bulk').exists())
        self.assertFalse(CustomScriptProjectRevision.objects.filter(pk=revision.pk).exists())

    def test_collecting_dependents_of_an_active_project_does_not_raise(self):
        # What the delete confirmation page does before any deletion happens. It ran the
        # collector, PROTECT fired, and the page refused with the project named as its own
        # dependent object.
        project = CustomScriptProject.objects.create(name='AR Collect', key='ar-collect')
        revision = CustomScriptProjectRevision.objects.create(project=project, digest=compute_digest([]))
        project.active_revision = revision
        project.save()
        collector = Collector(using=DEFAULT_DB_ALIAS)
        collector.collect([project])
        collected = {model for model, _instances in collector.instances_with_model()}
        self.assertIn(CustomScriptProjectRevision, collected)


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

    def test_entrypoint_fields_default_to_the_empty_snapshot(self):
        instance = self.make_revision()
        self.assertEqual(instance.entrypoint_snapshot, [])
        self.assertEqual(instance.entrypoint_digest, EMPTY_SNAPSHOT_DIGEST)

    def test_one_digest_is_allowed_under_two_entrypoint_configurations(self):
        # The same source tree under a changed configuration is a new, separately
        # validatable identity that reuses the stored content.
        first = self.make_revision()
        second = self.make_revision(entrypoint_digest='b' * 64)
        self.assertNotEqual(first.pk, second.pk)

    def test_the_identity_triple_is_refused_when_repeated(self):
        self.make_revision(entrypoint_digest='b' * 64)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_revision(entrypoint_digest='b' * 64)

    def test_entrypoint_snapshot_immutable_on_save(self):
        instance = self.make_revision()
        instance.entrypoint_snapshot = [{'module': 1, 'source_path': 'a.py'}]
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('entrypoint_snapshot', cm.exception.message_dict)

    def test_entrypoint_digest_immutable_on_save(self):
        instance = self.make_revision()
        instance.entrypoint_digest = 'b' * 64
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('entrypoint_digest', cm.exception.message_dict)

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
            constants.PENDING_VERDICT_REVISION_STATUSES,
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
        # A verdict is either still coming or already reached, never both.
        self.assertEqual(activatable & set(constants.PENDING_VERDICT_REVISION_STATUSES), set())
        # A rejected revision is terminal for the storage layer, which is what stops a
        # re-stage from clearing a validation verdict.
        self.assertNotIn(RevisionStatusChoices.INVALID, stored | retryable)
        self.assertNotIn(RevisionStatusChoices.MATERIALIZED, activatable)
        # Project validation owns VALIDATING, so the storage layer must never re-drive it.
        # Without this the validator's own transition would be reversible by a re-stage.
        self.assertNotIn(RevisionStatusChoices.VALIDATING, stored | retryable)


class CustomScriptProjectSourceStateTestCase(TestCase):
    """The source state a project reports for its detail view."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')

    def revision(self, status, digest=DIGEST_A, **kwargs):
        return CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=digest,
            status=status,
            manifest=[{'path': 'deploy.py', 'size': 1, 'sha256': DIGEST_A}] if digest else [],
            file_count=1 if digest else 0,
            total_size=1 if digest else 0,
            **kwargs,
        )

    def test_a_project_with_no_source_says_so(self):
        self.assertIn('No source', str(self.project.source_state))

    def test_the_newest_revision_is_returned_whatever_its_state(self):
        self.revision(RevisionStatusChoices.MATERIALIZED)
        # A rejected staging carries no digest, so current_revision skips it but this must not.
        rejected = self.revision(RevisionStatusChoices.INVALID, digest=None)
        self.assertEqual(self.project.latest_revision(), rejected)

    def test_the_active_revision_being_newest_is_reported_as_current(self):
        active = self.revision(RevisionStatusChoices.ACTIVE)
        self.project.active_revision = active
        self.project.save()
        self.assertIn('newest source', str(self.project.source_state))

    def test_a_valid_revision_awaiting_activation_is_reported(self):
        self.revision(RevisionStatusChoices.VALID)
        self.assertIn('waiting to be activated', str(self.project.source_state))

    def test_a_failed_validation_is_reported(self):
        self.revision(RevisionStatusChoices.INVALID, digest=None)
        self.assertIn('failed validation', str(self.project.source_state))

    def test_every_status_that_can_be_newest_and_not_active_has_a_summary(self):
        # A status with no summary falls through to the generic phrase, which tells an operator
        # nothing about what the project is waiting on. ACTIVE is excluded because a revision
        # cannot legitimately be ACTIVE without being the pointer: activation sets both in one
        # transaction, and that case is answered by the earlier branch instead.
        revision = self.revision(RevisionStatusChoices.STAGING)
        for status in RevisionStatusChoices.values():
            if status == RevisionStatusChoices.ACTIVE:
                continue
            with self.subTest(status=status):
                # Moved with update() rather than recreated, because this fixture's manifest is
                # not a real one, and the deletion signal refuses a revision whose manifest it
                # cannot validate. Status is not a frozen field, so the move is legitimate.
                CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(status=status)
                self.assertNotIn('A newer revision exists', str(self.project.source_state))

    def test_a_short_digest_is_the_prefix_a_revision_is_named_by(self):
        revision = self.revision(RevisionStatusChoices.MATERIALIZED)
        self.assertEqual(revision.short_digest, DIGEST_A[:12])

    def test_a_rejected_revision_has_no_short_digest(self):
        revision = self.revision(RevisionStatusChoices.INVALID, digest=None)
        self.assertEqual(revision.short_digest, '')
