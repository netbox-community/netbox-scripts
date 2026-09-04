from django.test import SimpleTestCase

from netbox_scripts.choices import FileDiscoveryStatusChoices
from netbox_scripts.graphql.enums import FileDiscoveryStatusEnum
from netbox_scripts.graphql.types import NetBoxScriptType, ScriptFileType


class NetBoxScriptGraphQLTestCase(SimpleTestCase):
    """Pin the GraphQL contract: the revision stays absent, the rest is readable."""

    @staticmethod
    def _field_names():
        return {field.name for field in NetBoxScriptType.__strawberry_definition__.fields}

    def test_last_seen_revision_is_exposed_on_object_type(self):
        self.assertIn('last_seen_revision', self._field_names())

    def test_the_administrator_and_derived_fields_are_readable(self):
        field_names = self._field_names()
        for name in ('module_path', 'class_name', 'display_name', 'enabled', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertIn(name, field_names)


class ScriptFileGraphQLTestCase(SimpleTestCase):
    """Pin the GraphQL contract: the discovery enum matches its ChoiceSet, the revision resolves."""

    def test_discovery_status_enum_matches_choices(self):
        self.assertEqual(
            {member.value for member in FileDiscoveryStatusEnum},
            set(FileDiscoveryStatusChoices.values()),
        )

    def test_last_discovered_revision_is_exposed_on_object_type(self):
        field_names = {field.name for field in ScriptFileType.__strawberry_definition__.fields}
        self.assertIn('last_discovered_revision', field_names)

    def test_discovery_status_exposed_as_a_raw_value(self):
        # Typed enums belong on filter inputs only.
        field = next(
            item for item in ScriptFileType.__strawberry_definition__.fields if item.name == 'discovery_status'
        )
        self.assertIsNot(field.type, FileDiscoveryStatusEnum)
