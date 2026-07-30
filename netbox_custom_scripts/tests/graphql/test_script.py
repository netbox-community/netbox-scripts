from django.test import SimpleTestCase

from netbox_custom_scripts.graphql.types import CustomScriptType


class CustomScriptGraphQLTestCase(SimpleTestCase):
    """Pin the GraphQL contract: the revision stays absent, the rest is readable."""

    @staticmethod
    def _field_names():
        return {field.name for field in CustomScriptType.__strawberry_definition__.fields}

    def test_last_seen_revision_not_exposed_on_object_type(self):
        # Revisions have no registered type, so strawberry cannot resolve the relation.
        self.assertNotIn('last_seen_revision', self._field_names())

    def test_the_administrator_and_derived_fields_are_readable(self):
        field_names = self._field_names()
        for name in ('module_path', 'class_name', 'display_name', 'enabled', 'is_retired', 'metadata'):
            with self.subTest(field=name):
                self.assertIn(name, field_names)
