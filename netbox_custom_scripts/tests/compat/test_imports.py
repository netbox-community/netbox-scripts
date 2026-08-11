import importlib.machinery
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

import dcim.models
import extras.models
import netbox_custom_scripts.scripts
from netbox_custom_scripts import compat
from netbox_custom_scripts.compat import compat_import, install, wrap_loader

AUTHORING_PACKAGE = 'netbox_custom_scripts.scripts'


class CompatImportTestCase(SimpleTestCase):
    def test_a_named_legacy_import_resolves_to_the_authoring_api(self):
        # from extras.scripts import Script
        self.assertIs(compat_import('extras.scripts', fromlist=('Script',)), netbox_custom_scripts.scripts)

    def test_a_wildcard_legacy_import_resolves_to_the_authoring_api(self):
        # from extras.scripts import *
        self.assertIs(compat_import('extras.scripts', fromlist=('*',)), netbox_custom_scripts.scripts)

    def test_a_dotted_legacy_import_binds_the_stand_in(self):
        # import extras.scripts, and import extras.scripts as api
        self.assertIs(compat_import('extras.scripts').scripts, netbox_custom_scripts.scripts)

    def test_importing_the_submodule_from_the_package_binds_the_stand_in(self):
        # from extras import scripts. The form a legacy-name-only map misses, which is why the
        # whole extras name resolves through the stand-in.
        self.assertIs(compat_import('extras', fromlist=('scripts',)).scripts, netbox_custom_scripts.scripts)

    def test_importing_the_package_alone_binds_the_stand_in(self):
        # import extras, then extras.scripts.Script
        self.assertIs(compat_import('extras').scripts, netbox_custom_scripts.scripts)

    def test_the_stand_in_delegates_every_other_attribute(self):
        self.assertIs(compat_import('extras').models, extras.models)

    def test_a_non_legacy_submodule_is_served_as_it_is(self):
        # from extras.models import Tag
        self.assertIs(compat_import('extras.models', fromlist=('Tag',)), extras.models)

    def test_the_legacy_reports_name_resolves_to_the_refused_marker(self):
        module = compat_import('extras.reports', fromlist=('Report',))
        self.assertTrue(issubclass(module.Report, netbox_custom_scripts.scripts.Script))
        self.assertTrue(module.Report._custom_script_report)

    def test_a_relative_import_is_never_intercepted(self):
        # A revision shipping its own extras.py reaches it with "from . import extras", and the
        # seam must hand that to the real machinery rather than answer with the stand-in.
        with self.assertRaises(ImportError):
            compat_import('extras', globals={'__package__': AUTHORING_PACKAGE}, fromlist=('x',), level=1)

    def test_a_relative_import_of_a_real_sibling_still_resolves(self):
        module = compat_import('base', globals={'__package__': AUTHORING_PACKAGE}, fromlist=('Script',), level=1)
        self.assertIs(module, netbox_custom_scripts.scripts.base)

    def test_every_unrelated_import_is_the_real_one(self):
        self.assertIs(compat_import('dcim.models', fromlist=('Device',)), dcim.models)


class TransitionTestCase(SimpleTestCase):
    """The branch that runs once the host stops shipping a legacy module, forced by the probe."""

    def test_a_legacy_import_is_refused_once_the_host_drops_the_module(self):
        with mock.patch.object(compat, '_host_provides', return_value=False), self.assertRaises(ImportError) as caught:
            compat_import('extras.scripts', fromlist=('Script',))
        self.assertIn(AUTHORING_PACKAGE, str(caught.exception))

    def test_the_refusal_survives_the_attribute_reach(self):
        # import extras, then extras.scripts. The stand-in is cached across both eras, so the
        # probe has to run on every reach rather than at build time.
        stand_in = compat_import('extras')
        with mock.patch.object(compat, '_host_provides', return_value=False), self.assertRaises(ImportError):
            getattr(stand_in, 'scripts')

    def test_the_report_refusal_names_no_replacement_module(self):
        with mock.patch.object(compat, '_host_provides', return_value=False), self.assertRaises(ImportError) as caught:
            compat_import('extras.reports', fromlist=('Report',))
        self.assertIn('Reports are not supported', str(caught.exception))
        self.assertNotIn('compat.legacy', str(caught.exception))

    def test_delegation_is_unaffected_by_the_transition(self):
        with mock.patch.object(compat, '_host_provides', return_value=False):
            self.assertIs(compat_import('extras').models, extras.models)

    def test_the_probe_reads_the_host_rather_than_a_version(self):
        self.assertTrue(compat._host_provides('extras.scripts'))
        self.assertFalse(compat._host_provides('extras.a_module_netbox_never_shipped'))


class InstallTestCase(SimpleTestCase):
    def test_the_host_module_is_never_replaced(self):
        # Concept 9.2: the mechanism replaces no unrelated module. On every supported version
        # NetBox ships its own extras.scripts and it stays exactly where it was.
        install()
        self.assertIsNot(sys.modules['extras.scripts'], netbox_custom_scripts.scripts)
        self.assertEqual(sys.modules['extras.scripts'].__name__, 'extras.scripts')

    def test_installing_twice_adds_one_finder(self):
        install()
        install()
        self.assertEqual(len([f for f in sys.meta_path if isinstance(f, compat._RevisionFinder)]), 1)

    def test_the_finder_is_installed_at_startup(self):
        self.assertTrue(any(isinstance(finder, compat._RevisionFinder) for finder in sys.meta_path))

    def test_the_finder_ignores_every_name_outside_the_private_namespace(self):
        self.assertIsNone(compat._RevisionFinder().find_spec('dcim.models'))

    def test_a_loader_that_runs_no_source_is_returned_unchanged(self):
        # The synthetic revision package branch builds a spec whose loader is None.
        self.assertIsNone(wrap_loader(None))

    def test_wrapping_a_wrapped_loader_returns_it_unchanged(self):
        loader = wrap_loader(importlib.machinery.SourceFileLoader('m', '/nonexistent/m.py'))
        self.assertIs(wrap_loader(loader), loader)

    def test_a_wrapped_loader_seeds_the_redirect_into_a_module(self):
        module = self.load_source('from extras.scripts import Script, StringVar\n')
        self.assertIs(module.Script, netbox_custom_scripts.scripts.Script)
        self.assertIs(module.StringVar, netbox_custom_scripts.scripts.StringVar)

    def test_an_unwrapped_loader_leaves_the_host_module_in_place(self):
        # The other half of the assertion above: without the wrap the same source reaches core,
        # which is the silence the seam removes.
        module = self.load_source('from extras.scripts import Script\n', wrap=False)
        self.assertIsNot(module.Script, netbox_custom_scripts.scripts.Script)

    def load_source(self, source, wrap=True):
        """Execute literal source through a file loader, wrapped or not, and return the module."""
        directory = Path(tempfile.mkdtemp(prefix='nbcs-compat-'))
        self.addCleanup(shutil.rmtree, directory, True)
        path = directory / 'seeded.py'
        path.write_text(source)
        # Never registered in sys.modules, so nothing needs unloading afterwards.
        spec = importlib.util.spec_from_file_location('nbcs_compat_probe', path)
        if wrap:
            spec.loader = wrap_loader(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
