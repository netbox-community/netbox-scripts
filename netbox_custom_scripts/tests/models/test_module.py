from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from netbox_custom_scripts.choices import ModuleDiscoveryStatusChoices, RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.utils import source_path_to_dotted_name

DIGEST_A = 'a' * 64


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


class CustomScriptModuleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Module Project 1', key='module-project-1')
        cls.other_project = CustomScriptProject.objects.create(name='Module Project 2', key='module-project-2')

    def test_create_customscriptmodule(self):
        instance = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        self.assertIsNotNone(instance.pk)
        self.assertTrue(instance.enabled)
        self.assertEqual(instance.discovery_status, ModuleDiscoveryStatusChoices.PENDING)
        self.assertEqual(instance.discovery_error, '')
        self.assertIsNone(instance.last_discovered_revision)

    def test_str(self):
        instance = CustomScriptModule(project=self.project, source_path='tools/deploy.py')
        self.assertEqual(str(instance), 'Module Project 1: tools/deploy.py')

    def test_the_discovery_fields_are_system_managed(self):
        # editable=False is what keeps them off every form and serializer.
        for field in ('discovery_status', 'discovery_error', 'last_discovered_revision'):
            with self.subTest(field=field):
                self.assertFalse(CustomScriptModule._meta.get_field(field).editable)

    def test_clean_canonicalizes_the_source_path(self):
        instance = CustomScriptModule(project=self.project, source_path='./tools//deploy.py')
        instance.full_clean()
        self.assertEqual(instance.source_path, 'tools/deploy.py')

    def test_save_canonicalizes_the_source_path(self):
        # Snapshot building reads rows straight from the ORM, so canonical form must hold
        # even for writes that never ran full_clean().
        instance = CustomScriptModule.objects.create(project=self.project, source_path='./tools/./deploy.py')
        self.assertEqual(instance.source_path, 'tools/deploy.py')

    def test_rejects_an_unsafe_source_path(self):
        for path in ('../outside.py', '/etc/deploy.py'):
            with self.subTest(path=path):
                instance = CustomScriptModule(project=self.project, source_path=path)
                with self.assertRaises(ValidationError) as cm:
                    instance.full_clean()
                self.assertIn('source_path', cm.exception.message_dict)

    def test_save_rejects_an_unsafe_source_path(self):
        instance = CustomScriptModule(project=self.project, source_path='../outside.py')
        with self.assertRaises(ValidationError) as cm:
            instance.save()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_rejects_a_path_that_cannot_be_imported(self):
        instance = CustomScriptModule(project=self.project, source_path='my-tools/deploy.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('source_path', cm.exception.message_dict)

    def test_rejects_a_case_folded_sibling_on_the_same_project(self):
        CustomScriptModule.objects.create(project=self.project, source_path='Utils.py')
        instance = CustomScriptModule(project=self.project, source_path='utils.py')
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('letter case is ignored', str(cm.exception.message_dict['source_path']))

    def test_allows_the_same_path_on_another_project(self):
        CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        instance = CustomScriptModule(project=self.other_project, source_path='deploy.py')
        instance.full_clean()
        instance.save()
        self.assertIsNotNone(instance.pk)

    def test_editing_a_module_does_not_collide_with_itself(self):
        instance = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        instance.description = 'Edited by the test suite.'
        instance.full_clean()

    def test_the_exact_duplicate_is_refused_by_the_database(self):
        CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        with transaction.atomic(), self.assertRaises(IntegrityError):
            CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')

    def test_last_discovered_revision_must_belong_to_the_project(self):
        foreign = CustomScriptProjectRevision.objects.create(
            project=self.other_project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        instance = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        instance.last_discovered_revision = foreign
        with self.assertRaises(ValidationError) as cm:
            instance.full_clean()
        self.assertIn('last_discovered_revision', cm.exception.message_dict)

    def test_last_discovered_revision_of_the_same_project_is_accepted(self):
        revision = CustomScriptProjectRevision.objects.create(
            project=self.project,
            digest=DIGEST_A,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        instance = CustomScriptModule.objects.create(project=self.project, source_path='deploy.py')
        instance.last_discovered_revision = revision
        instance.full_clean()
        instance.save()
        instance.refresh_from_db()
        self.assertEqual(instance.last_discovered_revision, revision)
