import importlib.machinery
import importlib.util

from django.test import TestCase

from netbox_scripts.runtime.discovery import DiscoveredScript, discover_scripts, zero_publication_reason
from netbox_scripts.runtime.exceptions import DiscoveryError
from netbox_scripts.runtime.naming import revision_module_name
from netbox_scripts.scripts import BaseScript, Script
from netbox_scripts.tests.runtime.test_loader import OTHER_KEY, LoaderTestCase
from netbox_scripts.tests.storage.test_store import STORAGE_KEY

DIGEST = '6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090'
PREFIX = revision_module_name(STORAGE_KEY, DIGEST)
PROJECT_KEY = 'automation'


class InstalledScript(Script):
    """A script class living in an installed package, which discovery must never publish."""


def make_module(name, source, **namespace):
    """Build one revision-shaped module from literal test source without touching sys.modules."""
    spec = importlib.machinery.ModuleSpec(name, None)
    module = importlib.util.module_from_spec(spec)
    module.__dict__.update(namespace)
    exec(compile(source, '<revision>', 'exec'), module.__dict__)  # noqa: S102
    return module


def discover(module):
    return discover_scripts(module, project_key=PROJECT_KEY, revision_prefix=PREFIX)


class DiscoverScriptsTestCase(TestCase):
    def test_script_file_defined_scripts_publish_alphabetically(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Beta(Script):\n    pass\n\nclass Alpha(Script):\n    pass\n',
            Script=Script,
        )
        found = discover(module)
        self.assertEqual([item.name for item in found], ['Alpha', 'Beta'])
        self.assertEqual([item.logical_module for item in found], ['deploy', 'deploy'])
        self.assertIsInstance(found[0], DiscoveredScript)

    def test_markers_land_in_the_class_dict_with_project_identity(self):
        module = make_module(f'{PREFIX}.deploy', 'class Sync(Script):\n    pass\n', Script=Script)
        (found,) = discover(module)
        self.assertEqual(found.cls.__dict__['_netbox_script_module'], 'deploy')
        self.assertEqual(
            found.cls.__dict__['_netbox_script_logger_name'],
            f'netbox.plugins.netbox_scripts.scripts.{PROJECT_KEY}.deploy.Sync',
        )

    def test_markers_carry_the_project_key_and_never_the_runtime_namespace(self):
        module = make_module(f'{PREFIX}.deploy', 'class Sync(Script):\n    pass\n', Script=Script)
        (found,) = discover(module)
        for marker in (found.cls.__dict__['_netbox_script_module'], found.cls.__dict__['_netbox_script_logger_name']):
            self.assertNotIn(DIGEST, marker)
            self.assertNotIn(STORAGE_KEY.hex, marker)
            self.assertNotIn('_netbox_scripts_runtime', marker)
        self.assertIn(f'.{PROJECT_KEY}.', found.cls.__dict__['_netbox_script_logger_name'])

    def test_a_helper_module_class_publishes_only_through_script_order(self):
        helpers = make_module(f'{PREFIX}.helpers', 'class Shared(Script):\n    pass\n', Script=Script)
        silent = make_module(
            f'{PREFIX}.deploy', 'class Local(Script):\n    pass\n', Script=Script, Shared=helpers.Shared
        )
        self.assertEqual([item.name for item in discover(silent)], ['Local'])
        listed = make_module(
            f'{PREFIX}.deploy',
            'script_order = [Shared]\n\nclass Local(Script):\n    pass\n',
            Script=Script,
            Shared=helpers.Shared,
        )
        found = discover(listed)
        self.assertEqual([item.name for item in found], ['Shared', 'Local'])
        self.assertEqual(found[0].logical_module, 'helpers')

    def test_script_order_fixes_the_order_and_the_rest_follows_alphabetically(self):
        helpers = make_module(f'{PREFIX}.helpers', 'class Zeta(Script):\n    pass\n', Script=Script)
        module = make_module(
            f'{PREFIX}.deploy',
            'class Mid(Script):\n    pass\n\nclass Alpha(Script):\n    pass\n\n'
            'class Beta(Script):\n    pass\n\nscript_order = [Zeta, Mid]\n',
            Script=Script,
            Zeta=helpers.Zeta,
        )
        self.assertEqual([item.name for item in discover(module)], ['Zeta', 'Mid', 'Alpha', 'Beta'])

    def test_aliases_collapse_to_one_publication(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Sync(Script):\n    pass\n\nAlias = Sync\nAnother = Sync\n',
            Script=Script,
        )
        found = discover(module)
        self.assertEqual([item.name for item in found], ['Sync'])

    def test_installed_package_classes_are_never_auto_published(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Local(Script):\n    pass\n',
            Script=Script,
            Imported=InstalledScript,
        )
        self.assertEqual([item.name for item in discover(module)], ['Local'])

    def test_an_installed_package_class_in_script_order_is_refused(self):
        module = make_module(f'{PREFIX}.deploy', 'script_order = [Imported]\n', Imported=InstalledScript)
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'not_revision_local')
        self.assertEqual(caught.exception.name, 'InstalledScript')

    def test_a_root_package_class_in_script_order_is_refused(self):
        root = make_module(PREFIX, 'class AtRoot(Script):\n    pass\n', Script=Script)
        module = make_module(f'{PREFIX}.deploy', 'script_order = [AtRoot]\n', AtRoot=root.AtRoot)
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'not_revision_local')

    def test_a_non_script_entry_in_script_order_is_refused(self):
        for entry_source in ('script_order = ["Sync"]\n', 'script_order = [object]\n'):
            with self.subTest(entry=entry_source):
                module = make_module(f'{PREFIX}.deploy', entry_source)
                with self.assertRaises(DiscoveryError) as caught:
                    discover(module)
                self.assertEqual(caught.exception.code, 'not_a_script')

    def test_a_base_script_building_block_is_not_published(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Block(BaseScript):\n    pass\n\nclass Real(Script):\n    pass\n',
            Script=Script,
            BaseScript=BaseScript,
        )
        self.assertEqual([item.name for item in discover(module)], ['Real'])

    def test_a_repeated_script_order_entry_is_refused(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Sync(Script):\n    pass\n\nscript_order = [Sync, Sync]\n',
            Script=Script,
        )
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'duplicate_entry')

    def test_a_script_order_that_is_not_a_sequence_is_refused(self):
        module = make_module(f'{PREFIX}.deploy', 'script_order = "Sync"\n')
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'invalid_script_order')

    def test_two_classes_sharing_one_identity_are_refused(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Foo(Script):\n    pass\n\n_first = Foo\n\nclass Foo(Script):\n    pass\n\n'
            'script_order = [_first, Foo]\n',
            Script=Script,
        )
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'duplicate_identity')
        self.assertEqual(caught.exception.name, 'Foo')

    def test_a_class_declaring_tests_and_no_run_is_refused(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Audit(Script):\n    def test_names(self):\n        pass\n',
            Script=Script,
        )
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'report_style')
        self.assertEqual(caught.exception.name, 'Audit')

    def test_a_class_carrying_the_report_marker_is_refused(self):
        # The marker the legacy Report base class sets, read by attribute so discovery stays
        # independent of the compatibility layer.
        module = make_module(
            f'{PREFIX}.deploy',
            'class Marked(Script):\n    _netbox_script_report = True\n\n'
            '    def run(self, data, commit):\n        pass\n',
            Script=Script,
        )
        with self.assertRaises(DiscoveryError) as caught:
            discover(module)
        self.assertEqual(caught.exception.code, 'report_style')
        self.assertEqual(caught.exception.name, 'Marked')

    def test_a_test_prefixed_attribute_does_not_refuse_a_script(self):
        module = make_module(f'{PREFIX}.deploy', 'class Runner(Script):\n    test_mode = True\n', Script=Script)
        self.assertEqual([item.name for item in discover(module)], ['Runner'])

    def test_a_script_declaring_tests_and_a_run_still_publishes(self):
        module = make_module(
            f'{PREFIX}.deploy',
            'class Runner(Script):\n    def test_names(self):\n        pass\n\n'
            '    def run(self, data, commit):\n        pass\n',
            Script=Script,
        )
        self.assertEqual([item.name for item in discover(module)], ['Runner'])

    def test_an_empty_module_publishes_nothing(self):
        module = make_module(f'{PREFIX}.deploy', 'VALUE = 1\n')
        self.assertEqual(discover(module), [])


class DiscoveryIntegrationTestCase(LoaderTestCase):
    SOURCE = b'from netbox_scripts.scripts import Script\n\n\nclass Sync(Script):\n    pass\n'

    def test_identical_names_in_two_projects_get_distinct_loggers(self):
        module_a, _, digest_a = self.load(STORAGE_KEY, {'deploy.py': self.SOURCE}, 'deploy.py')
        module_b, _, digest_b = self.load(OTHER_KEY, {'deploy.py': self.SOURCE}, 'deploy.py')
        (found_a,) = discover_scripts(
            module_a, project_key='alpha', revision_prefix=revision_module_name(STORAGE_KEY, digest_a)
        )
        (found_b,) = discover_scripts(
            module_b, project_key='beta', revision_prefix=revision_module_name(OTHER_KEY, digest_b)
        )
        logger_a = found_a.cls().logger.name
        logger_b = found_b.cls().logger.name
        self.assertNotEqual(logger_a, logger_b)
        self.assertEqual(logger_a, 'netbox.plugins.netbox_scripts.scripts.alpha.deploy.Sync')
        self.assertEqual(logger_b, 'netbox.plugins.netbox_scripts.scripts.beta.deploy.Sync')
        self.assertEqual(found_a.cls.full_name, 'deploy.Sync')
        self.assertEqual(found_a.cls.module, 'deploy')
        for private in (digest_a, digest_b, '_netbox_scripts_runtime'):
            self.assertNotIn(private, logger_a)
            self.assertNotIn(private, logger_b)


class ZeroPublicationReasonTestCase(TestCase):
    """Why a script file that imported cleanly still published nothing."""

    def test_a_host_based_class_is_named_with_its_migration_hint(self):
        # The base is crafted rather than imported, so the rule is exercised without this test
        # depending on a host module that is scheduled to go away.
        host_script = type('Script', (), {'__module__': 'extras.scripts'})
        module = make_module(f'{PREFIX}.deploy', 'class NewIP(Script):\n    pass\n', Script=host_script)

        self.assertEqual(
            zero_publication_reason(module),
            '"NewIP" subclasses extras.scripts.Script, which belongs to NetBox Community rather '
            'than to this plugin. Import the authoring API from "netbox_scripts.scripts" instead.',
        )

    def test_a_host_report_base_is_named_too(self):
        host_report = type('Report', (), {'__module__': 'extras.reports'})
        module = make_module(f'{PREFIX}.audit', 'class Stale(Report):\n    pass\n', Report=host_report)

        self.assertIn('extras.reports.Report', zero_publication_reason(module))
        self.assertIn('Reports are not supported.', zero_publication_reason(module))

    def test_a_module_defining_nothing_gets_the_generic_reason(self):
        module = make_module(f'{PREFIX}.deploy', 'VALUE = 1\n')

        self.assertEqual(
            zero_publication_reason(module),
            'The module imports cleanly and defines no Script.',
        )

    def test_a_helper_defining_plain_classes_gets_the_generic_reason(self):
        module = make_module(f'{PREFIX}.helpers', 'class Formatter:\n    pass\n')

        self.assertEqual(
            zero_publication_reason(module),
            'The module imports cleanly and defines no Script.',
        )

    def test_a_class_from_an_installed_package_is_not_blamed(self):
        # InstalledScript is imported rather than defined here, so it says nothing about why
        # this module published nothing.
        module = make_module(f'{PREFIX}.deploy', 'BORROWED = InstalledScript\n', InstalledScript=InstalledScript)

        self.assertEqual(
            zero_publication_reason(module),
            'The module imports cleanly and defines no Script.',
        )
