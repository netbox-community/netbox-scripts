import json

from django.test import TestCase

from netbox_custom_scripts.runtime.exceptions import ScriptMetadataError
from netbox_custom_scripts.runtime.introspection import (
    RESERVED_VARIABLE_NAMES,
    describe_script,
    validate_discovered_scripts,
)
from netbox_custom_scripts.scripts import Script
from netbox_custom_scripts.scripts.forms import ScriptForm
from netbox_custom_scripts.scripts.variables import ScriptVariable, StringVar
from netbox_custom_scripts.tests.runtime.test_discovery import PREFIX, discover, make_module

MODULE_ID = 12
ENTRYPOINT = 'main.py'


def describe(module, position=0):
    """Describe every class one revision module publishes, in publication order."""
    return [
        describe_script(item, entrypoint_module_id=MODULE_ID, entrypoint_path=ENTRYPOINT, position=position + offset)
        for offset, item in enumerate(discover(module))
    ]


def build(source, **namespace):
    """Build one revision entrypoint from literal source, with the authoring API in scope."""
    namespace.setdefault('Script', Script)
    namespace.setdefault('StringVar', StringVar)
    namespace.setdefault('ScriptVariable', ScriptVariable)
    return make_module(f'{PREFIX}.{ENTRYPOINT[:-3]}', source, **namespace)


class DescribeScriptTestCase(TestCase):
    def test_a_record_carries_the_identity_and_its_entrypoint_provenance(self):
        module = build('class Sync(Script):\n    pass\n')
        (record,) = describe(module)
        self.assertEqual(record['module_path'], 'main')
        self.assertEqual(record['class_name'], 'Sync')
        self.assertEqual(record['entrypoint_module_id'], MODULE_ID)
        self.assertEqual(record['entrypoint_path'], ENTRYPOINT)
        self.assertEqual(record['position'], 0)

    def test_a_helper_defined_class_records_where_it_is_defined_not_what_published_it(self):
        helpers = make_module(f'{PREFIX}.helpers', 'class Shared(Script):\n    pass\n', Script=Script)
        module = build('script_order = [Shared]\n', Shared=helpers.Shared)
        (record,) = describe(module)
        self.assertEqual(record['module_path'], 'helpers')
        self.assertEqual(record['class_name'], 'Shared')
        # The declaration that published it stays recorded, so provenance survives without
        # the row identity depending on an entrypoint that may later be removed.
        self.assertEqual(record['entrypoint_path'], ENTRYPOINT)
        self.assertEqual(record['entrypoint_module_id'], MODULE_ID)

    def test_the_display_name_and_description_come_from_meta(self):
        module = build(
            'class Sync(Script):\n'
            '    class Meta:\n'
            "        name = 'Deploy Devices'\n"
            "        description = 'Deploy devices at a site.'\n"
        )
        (record,) = describe(module)
        self.assertEqual(record['display_name'], 'Deploy Devices')
        self.assertEqual(record['description'], 'Deploy devices at a site.')

    def test_a_silent_meta_falls_back_to_the_class_name_and_no_description(self):
        module = build('class Sync(Script):\n    pass\n')
        (record,) = describe(module)
        self.assertEqual(record['display_name'], 'Sync')
        self.assertEqual(record['description'], '')

    def test_the_metadata_carries_every_execution_default(self):
        module = build('class Sync(Script):\n    pass\n')
        (record,) = describe(module)
        self.assertEqual(
            record['metadata'],
            {
                'commit_default': True,
                'scheduling_enabled': True,
                'job_timeout': None,
                'notifications_default': 'always',
            },
        )

    def test_the_metadata_reflects_an_authored_meta(self):
        module = build(
            'class Sync(Script):\n'
            '    class Meta:\n'
            '        commit_default = False\n'
            '        scheduling_enabled = False\n'
            '        job_timeout = 300\n'
            "        notifications_default = 'never'\n"
        )
        (record,) = describe(module)
        self.assertEqual(
            record['metadata'],
            {
                'commit_default': False,
                'scheduling_enabled': False,
                'job_timeout': 300,
                'notifications_default': 'never',
            },
        )

    def test_a_job_timeout_that_is_not_a_number_is_refused(self):
        module = build('class Sync(Script):\n    class Meta:\n        job_timeout = "soon"\n')
        with self.assertRaises(ScriptMetadataError) as captured:
            describe(module)
        self.assertEqual(captured.exception.code, 'invalid_job_timeout')

    def test_the_record_is_json_safe(self):
        module = build('class Sync(Script):\n    alpha = StringVar()\n')
        (record,) = describe(module)
        self.assertEqual(json.loads(json.dumps(record)), record)


class RunFormValidationTestCase(TestCase):
    def test_the_reserved_names_are_derived_from_the_run_form(self):
        # Spelled-out names would silently stop covering a field added to the run form.
        self.assertEqual(RESERVED_VARIABLE_NAMES, frozenset(ScriptForm.declared_fields))
        self.assertIn('_commit', RESERVED_VARIABLE_NAMES)

    def test_a_variable_shadowing_a_run_form_field_is_refused(self):
        module = build('class Sync(Script):\n    _commit = StringVar()\n')
        with self.assertRaises(ScriptMetadataError) as captured:
            describe(module)
        self.assertEqual(captured.exception.code, 'reserved_variable_name')
        self.assertEqual(captured.exception.name, '_commit')

    def test_a_variable_django_cannot_build_a_field_from_is_refused(self):
        module = build(
            'class Broken(ScriptVariable):\n'
            '    def __init__(self):\n'
            '        super().__init__()\n'
            "        self.field_attrs['max_digits'] = 4\n"
            '\n'
            'class Sync(Script):\n'
            '    alpha = Broken()\n'
        )
        with self.assertRaises(ScriptMetadataError) as captured:
            describe(module)
        self.assertEqual(captured.exception.code, 'form_construction_failed')
        self.assertEqual(captured.exception.name, 'Sync')
        # The original is chained, so the classifier and the operator both see the real cause.
        self.assertIsInstance(captured.exception.__cause__, TypeError)

    def test_a_fieldset_naming_something_that_is_not_a_variable_is_refused(self):
        module = build(
            'class Sync(Script):\n'
            '    alpha = StringVar()\n'
            '\n'
            '    class Meta:\n'
            "        fieldsets = (('Data', ('alpha', 'missing')),)\n"
        )
        with self.assertRaises(ScriptMetadataError) as captured:
            describe(module)
        self.assertEqual(captured.exception.code, 'unknown_fieldset_field')
        self.assertEqual(captured.exception.name, 'missing')

    def test_a_fieldset_naming_the_commit_toggle_is_accepted(self):
        module = build(
            'class Sync(Script):\n'
            '    alpha = StringVar()\n'
            '\n'
            '    class Meta:\n'
            "        fieldsets = (('Data', ('alpha', '_commit')),)\n"
        )
        (record,) = describe(module)
        self.assertEqual(record['class_name'], 'Sync')

    def test_a_class_name_too_long_to_store_is_refused(self):
        module = build(f'class {"A" * 80}(Script):\n    pass\n')
        with self.assertRaises(ScriptMetadataError) as captured:
            describe(module)
        self.assertEqual(captured.exception.code, 'identity_too_long')

    def test_a_class_name_at_the_limit_is_accepted(self):
        name = 'A' * 79
        module = build(f'class {name}(Script):\n    pass\n')
        (record,) = describe(module)
        self.assertEqual(record['class_name'], name)


class ValidateDiscoveredScriptsTestCase(TestCase):
    def setUp(self):
        module = build('class Alpha(Script):\n    pass\n\nclass Beta(Script):\n    pass\n')
        self.snapshot = describe(module)

    def test_a_built_snapshot_survives_the_return_trip_unchanged(self):
        self.assertEqual(validate_discovered_scripts(self.snapshot), self.snapshot)

    def test_an_empty_snapshot_is_accepted(self):
        self.assertEqual(validate_discovered_scripts([]), [])

    def test_a_value_that_is_not_a_list_is_refused(self):
        for value in ({}, '', None, 0):
            with self.subTest(value=value), self.assertRaises(ScriptMetadataError) as captured:
                validate_discovered_scripts(value)
            self.assertEqual(captured.exception.code, 'invalid_snapshot')

    def test_an_entry_that_is_not_an_object_is_refused(self):
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts([['module_path', 'main']])
        self.assertEqual(captured.exception.code, 'invalid_entry')

    def test_a_missing_text_field_is_refused(self):
        for key in ('module_path', 'class_name', 'entrypoint_path', 'display_name', 'description'):
            damaged = [dict(self.snapshot[0])]
            del damaged[0][key]
            with self.subTest(key=key), self.assertRaises(ScriptMetadataError) as captured:
                validate_discovered_scripts(damaged)
            self.assertEqual(captured.exception.code, 'invalid_entry')
            self.assertEqual(captured.exception.name, key)

    def test_a_non_string_text_field_is_refused(self):
        damaged = [dict(self.snapshot[0])]
        damaged[0]['class_name'] = 17
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'invalid_entry')

    def test_a_position_that_is_not_the_index_is_refused(self):
        damaged = [dict(record) for record in self.snapshot]
        damaged[1]['position'] = 0
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'invalid_position')

    def test_a_reordered_snapshot_is_refused(self):
        # Order carries presentation order, so reversing the list without renumbering is a
        # different claim than the one validation made.
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(list(reversed(self.snapshot)))
        self.assertEqual(captured.exception.code, 'invalid_position')

    def test_metadata_that_is_not_an_object_is_refused(self):
        damaged = [dict(self.snapshot[0])]
        damaged[0]['metadata'] = []
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'invalid_entry')
        self.assertEqual(captured.exception.name, 'metadata')

    def test_an_over_long_identity_is_refused(self):
        damaged = [dict(self.snapshot[0])]
        damaged[0]['class_name'] = 'A' * 80
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'identity_too_long')

    def test_an_over_long_module_path_is_refused(self):
        damaged = [dict(self.snapshot[0])]
        damaged[0]['module_path'] = 'a' * 1001
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'identity_too_long')

    def test_a_duplicate_identity_is_refused(self):
        damaged = [dict(self.snapshot[0]), dict(self.snapshot[0])]
        damaged[1]['position'] = 1
        with self.assertRaises(ScriptMetadataError) as captured:
            validate_discovered_scripts(damaged)
        self.assertEqual(captured.exception.code, 'duplicate_identity')
