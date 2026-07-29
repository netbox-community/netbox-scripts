import uuid

from django.test import TestCase

from netbox_custom_scripts.runtime.exceptions import InvalidModulePathError
from netbox_custom_scripts.runtime.naming import (
    PRIVATE_ROOT,
    entrypoint_dotted_name,
    project_module_name,
    revision_module_name,
)
from netbox_custom_scripts.storage.exceptions import UnsafePathError

STORAGE_KEY = uuid.UUID('9f1c6d24-0b2a-4d3e-8f57-2c9a4b6e1d80')
OTHER_KEY = uuid.UUID('5b7e2f10-9c4d-4a6b-b1e8-7d3f5a2c9e41')
DIGEST = '6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090'
OTHER_DIGEST = '3fdba35f04dc8c462986c992bcf875546257113072a909c162f7e470e581e278'


class ModuleNameTestCase(TestCase):
    def test_the_private_root_is_an_underscored_identifier(self):
        self.assertTrue(PRIVATE_ROOT.isidentifier())
        self.assertTrue(PRIVATE_ROOT.startswith('_'))

    def test_a_project_name_embeds_the_storage_key_as_hex(self):
        self.assertEqual(project_module_name(STORAGE_KEY), f'{PRIVATE_ROOT}.p_{STORAGE_KEY.hex}')

    def test_every_name_segment_is_importable(self):
        for name in (project_module_name(STORAGE_KEY), revision_module_name(STORAGE_KEY, DIGEST)):
            with self.subTest(name=name):
                self.assertTrue(all(segment.isidentifier() for segment in name.split('.')))

    def test_a_string_storage_key_names_the_same_module(self):
        self.assertEqual(project_module_name(str(STORAGE_KEY)), project_module_name(STORAGE_KEY))

    def test_names_are_stable_across_calls(self):
        self.assertEqual(project_module_name(STORAGE_KEY), project_module_name(STORAGE_KEY))
        self.assertEqual(revision_module_name(STORAGE_KEY, DIGEST), revision_module_name(STORAGE_KEY, DIGEST))

    def test_a_revision_name_nests_under_its_project(self):
        name = revision_module_name(STORAGE_KEY, DIGEST)
        self.assertEqual(name, f'{project_module_name(STORAGE_KEY)}.r_{DIGEST}')

    def test_names_differ_across_projects_and_revisions(self):
        self.assertNotEqual(project_module_name(STORAGE_KEY), project_module_name(OTHER_KEY))
        self.assertNotEqual(revision_module_name(STORAGE_KEY, DIGEST), revision_module_name(STORAGE_KEY, OTHER_DIGEST))
        self.assertNotEqual(revision_module_name(STORAGE_KEY, DIGEST), revision_module_name(OTHER_KEY, DIGEST))


class EntrypointDottedNameTestCase(TestCase):
    def test_a_module_path_maps_to_its_dotted_name(self):
        self.assertEqual(entrypoint_dotted_name('deploy.py'), 'deploy')
        self.assertEqual(entrypoint_dotted_name('tools/deploy.py'), 'tools.deploy')

    def test_a_subpackage_init_maps_to_the_package_name(self):
        self.assertEqual(entrypoint_dotted_name('pkg/__init__.py'), 'pkg')

    def test_rejected_paths_carry_the_converter_code(self):
        cases = (
            ('deploy.txt', 'not_a_python_file'),
            ('__init__.py', 'root_entrypoint'),
            ('tools/my-script.py', 'invalid_identifier'),
            ('class/deploy.py', 'reserved_keyword'),
        )
        for path, code in cases:
            with self.subTest(path=path), self.assertRaises(InvalidModulePathError) as caught:
                entrypoint_dotted_name(path)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.path, path)

    def test_the_rejection_is_an_unsafe_path_error(self):
        with self.assertRaises(UnsafePathError):
            entrypoint_dotted_name('__init__.py')
