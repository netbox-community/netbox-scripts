from django.test import SimpleTestCase

from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.graphql.enums import RevisionStatusEnum
from netbox_scripts.graphql.types import CustomScriptProjectRevisionType


class CustomScriptProjectRevisionGraphQLTestCase(SimpleTestCase):
    """Pin the GraphQL contract: the status enum matches its ChoiceSet, stored documents stay off."""

    def test_status_enum_matches_choices(self):
        self.assertEqual(
            {member.value for member in RevisionStatusEnum},
            set(RevisionStatusChoices.values()),
        )

    def test_status_exposed_as_a_raw_value(self):
        # Typed enums belong on filter inputs only.
        field = next(
            item for item in CustomScriptProjectRevisionType.__strawberry_definition__.fields if item.name == 'status'
        )
        self.assertIsNot(field.type, RevisionStatusEnum)

    def test_stored_documents_and_lease_fields_are_not_exposed(self):
        field_names = {field.name for field in CustomScriptProjectRevisionType.__strawberry_definition__.fields}
        for absent in ('manifest', 'entrypoint_snapshot', 'validation_job', 'validation_started'):
            with self.subTest(field=absent):
                self.assertNotIn(absent, field_names)

    def test_the_project_relation_is_exposed(self):
        field_names = {field.name for field in CustomScriptProjectRevisionType.__strawberry_definition__.fields}
        self.assertIn('project', field_names)
