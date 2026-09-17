from django.test import SimpleTestCase

from netbox_scripts.compat import LEGACY_MODULES
from netbox_scripts.migration import dialects

NATIVE_SCRIPT = b"""from netbox_scripts.scripts import Script


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

NATIVE_SCRIPT_WITH_A_TEST_METHOD = b"""from netbox_scripts.scripts import Script


class Deploy(Script):
    def test_every_device_has_a_platform(self):
        pass

    def run(self, data, commit):
        pass
"""

INHERITED_RUN = b"""from netbox_scripts.scripts import Script


class DeviceScript(Script):
    def run(self, data, commit):
        pass


class Deploy(DeviceScript):
    def test_connectivity(self):
        pass
"""

LEGACY_INHERITED_RUN = b"""from extras.scripts import Script


class DeviceScript(Script):
    def run(self, data, commit):
        pass


class Deploy(DeviceScript):
    def test_connectivity(self):
        pass
"""

SUBCLASS_OF_AN_UNSEEN_BASE = b"""from mytools.checks import DeviceCheck


class Audit(DeviceCheck):
    def test_every_device_has_a_platform(self):
        pass
"""

SCRIPT_SUBCLASS_WITHOUT_RUN = b"""from netbox_scripts.scripts import Script


class Deploy(Script):
    description = 'Runs whatever its base runs.'
"""

REPORT_UNDER_THE_BARE_PACKAGE_IMPORT = b"""import extras


class Audit(extras.reports.Report):
    def test_every_device_has_a_platform(self):
        pass
"""

REPORT_BASE_WITHOUT_AN_IMPORT = b"""class Audit(Report):
    def test_every_device_has_a_platform(self):
        pass
"""

REPORT_SHAPE_UNDER_OBJECT = b"""class Audit(object):
    def test_every_device_has_a_platform(self):
        pass
"""

RUN_FROM_A_LOCAL_BASE_NAMED_REPORT = b"""class Report:
    def run(self, data, commit):
        pass


class Audit(Report):
    def test_every_device_has_a_platform(self):
        pass
"""

A_NESTED_NAMESAKE_WITH_RUN = b"""class Audit:
    def test_every_device_has_a_platform(self):
        pass


def factory():
    class Audit:
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

    def test_a_run_method_inherited_from_a_base_here_is_not_report_shape(self):
        # An intermediate base supplies run and the subclass adds a helper. Discovery publishes
        # this class and the body-only check called it a Report.
        self.assertEqual(dialects.classify(INHERITED_RUN), dialects.NATIVE)

    def test_the_same_shape_under_the_built_in_import_is_only_a_legacy_import(self):
        # The form an operator actually has, and report shape would block the whole staging pass.
        self.assertEqual(dialects.classify(LEGACY_INHERITED_RUN), dialects.LEGACY_IMPORT)

    def test_test_methods_under_a_base_this_module_cannot_see_are_not_report_shape(self):
        self.assertEqual(dialects.classify(SUBCLASS_OF_AN_UNSEEN_BASE), dialects.NATIVE)

    def test_unparsable_source_is_reported_as_itself(self):
        self.assertEqual(dialects.classify(b'def broken(\n'), dialects.UNPARSABLE)

    def test_every_compat_legacy_name_is_detected(self):
        # The compat layer owns the legacy name set, so a form added there cannot go unreported.
        for name in LEGACY_MODULES:
            with self.subTest(name=name):
                self.assertNotEqual(dialects.classify(f'import {name}\n'.encode()), dialects.NATIVE)

    def test_a_report_base_reached_through_the_bare_package_import_is_report_shape(self):
        self.assertEqual(dialects.classify(REPORT_UNDER_THE_BARE_PACKAGE_IMPORT), dialects.REPORT_STYLE)

    def test_a_base_named_report_the_module_never_imports_is_report_shape(self):
        self.assertEqual(dialects.classify(REPORT_BASE_WITHOUT_AN_IMPORT), dialects.REPORT_STYLE)

    def test_test_methods_under_object_are_report_shape(self):
        self.assertEqual(dialects.classify(REPORT_SHAPE_UNDER_OBJECT), dialects.REPORT_STYLE)

    def test_a_local_base_named_report_that_carries_run_is_not_report_shape(self):
        # Declared here, so its body is read rather than its name assumed.
        self.assertEqual(dialects.classify(RUN_FROM_A_LOCAL_BASE_NAMED_REPORT), dialects.NATIVE)

    def test_a_nested_namesake_does_not_hide_a_report_shaped_class(self):
        self.assertEqual(dialects.classify(A_NESTED_NAMESAKE_WITH_RUN), dialects.REPORT_STYLE)

    def test_a_type_checking_report_import_is_not_report_shape(self):
        # Report style is blocking, so misreading one annotation import would stop the whole
        # staging pass over a module that needs no rewrite at all.
        source = b'from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from extras import reports\n'
        self.assertEqual(dialects.classify(source), dialects.NATIVE)

    def test_a_type_checking_legacy_import_nested_in_a_live_else_is_native(self):
        # Skipping one guarded body must not stop the walker recognising the next guard inside
        # the branch it selected.
        source = (
            b'from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    pass\n'
            b'else:\n    if TYPE_CHECKING:\n        from extras import reports\n'
        )
        self.assertEqual(dialects.classify(source), dialects.NATIVE)

    def test_a_constant_false_legacy_import_is_native(self):
        self.assertEqual(dialects.classify(b'if False:\n    import extras\n'), dialects.NATIVE)

    def test_a_legacy_import_inside_a_function_body_is_still_legacy(self):
        # Deferred is not unreachable. The author still owes the one-line edit here.
        self.assertEqual(dialects.classify(b'def f():\n    import extras\n'), dialects.LEGACY_IMPORT)

    def test_a_condition_this_cannot_read_leaves_the_import_counted(self):
        source = b'import os\n\nif os.environ.get("FEATURE"):\n    import extras\n'
        self.assertEqual(dialects.classify(source), dialects.LEGACY_IMPORT)


class DefinesAScriptTestCase(SimpleTestCase):
    """Whether stored source declares something that could publish, decided without importing it."""

    def test_a_run_method_of_its_own_publishes(self):
        self.assertTrue(dialects.defines_a_script(NATIVE_SCRIPT))

    def test_a_script_subclass_with_no_run_of_its_own_publishes(self):
        self.assertTrue(dialects.defines_a_script(SCRIPT_SUBCLASS_WITHOUT_RUN))

    def test_a_class_under_a_base_this_module_cannot_see_does_not_publish(self):
        # The opposite answer to the report check on the same source: an unseen base is not
        # evidence of a script, or every helper class in the tree would be declared.
        self.assertFalse(dialects.defines_a_script(SUBCLASS_OF_AN_UNSEEN_BASE))

    def test_a_module_of_plain_helpers_does_not_publish(self):
        self.assertFalse(dialects.defines_a_script(b'def render(config):\n    return config\n'))

    def test_unparsable_source_publishes_rather_than_migrating_in_silence(self):
        self.assertTrue(dialects.defines_a_script(b'def broken(\n'))
