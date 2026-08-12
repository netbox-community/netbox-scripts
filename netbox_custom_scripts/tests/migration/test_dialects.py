from django.test import SimpleTestCase

from netbox_custom_scripts.compat import LEGACY_MODULES
from netbox_custom_scripts.migration import dialects

NATIVE_SCRIPT = b"""from netbox_custom_scripts.scripts import Script


class Deploy(Script):
    def run(self, data, commit):
        pass
"""

REPORT_WITH_A_LEGACY_IMPORT = b"""from extras.reports import Report


class Audit(Report):
    def test_every_device_has_a_platform(self):
        pass
"""

REPORT_SHAPE_WITHOUT_AN_IMPORT = b"""class Audit:
    def test_every_device_has_a_platform(self):
        pass
"""

NATIVE_SCRIPT_WITH_A_TEST_METHOD = b"""from netbox_custom_scripts.scripts import Script


class Deploy(Script):
    def test_every_device_has_a_platform(self):
        pass

    def run(self, data, commit):
        pass
"""


class ClassifyTestCase(SimpleTestCase):
    """Authoring dialect of stored source, decided without importing it."""

    def test_a_plugin_import_is_native(self):
        self.assertEqual(dialects.classify(NATIVE_SCRIPT), dialects.NATIVE)

    def test_a_named_legacy_import_is_legacy(self):
        # from extras.scripts import Script
        self.assertEqual(dialects.classify(b'from extras.scripts import Script\n'), dialects.LEGACY_IMPORT)

    def test_importing_the_submodule_from_the_package_is_legacy(self):
        # from extras import scripts, the form a dotted-name-only check misses.
        self.assertEqual(dialects.classify(b'from extras import scripts\n'), dialects.LEGACY_IMPORT)

    def test_importing_the_package_alone_is_legacy(self):
        # import extras, then extras.scripts.Script
        self.assertEqual(dialects.classify(b'import extras\n'), dialects.LEGACY_IMPORT)

    def test_a_dotted_legacy_import_is_legacy(self):
        self.assertEqual(dialects.classify(b'import extras.scripts\n'), dialects.LEGACY_IMPORT)

    def test_an_aliased_legacy_import_is_legacy(self):
        self.assertEqual(dialects.classify(b'import extras.scripts as api\n'), dialects.LEGACY_IMPORT)

    def test_report_shape_outranks_its_legacy_import(self):
        self.assertEqual(dialects.classify(REPORT_WITH_A_LEGACY_IMPORT), dialects.REPORT_STYLE)

    def test_test_methods_without_a_run_method_are_report_shape(self):
        self.assertEqual(dialects.classify(REPORT_SHAPE_WITHOUT_AN_IMPORT), dialects.REPORT_STYLE)

    def test_a_test_method_beside_run_is_not_report_shape(self):
        self.assertEqual(dialects.classify(NATIVE_SCRIPT_WITH_A_TEST_METHOD), dialects.NATIVE)

    def test_unparsable_source_is_reported_as_itself(self):
        self.assertEqual(dialects.classify(b'def broken(\n'), dialects.UNPARSABLE)

    def test_every_compat_legacy_name_is_detected(self):
        # The compat layer owns the legacy name set, so a form added there cannot go unreported.
        for name in LEGACY_MODULES:
            with self.subTest(name=name):
                self.assertNotEqual(dialects.classify(f'import {name}\n'.encode()), dialects.NATIVE)
