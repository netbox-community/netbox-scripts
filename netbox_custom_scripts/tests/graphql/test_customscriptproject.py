from django.test import SimpleTestCase

from netbox_custom_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_custom_scripts.graphql.enums import ActivationPolicyEnum, ProjectSourceTypeEnum
from netbox_custom_scripts.graphql.types import CustomScriptProjectType


class CustomScriptProjectGraphQLTestCase(SimpleTestCase):
    """Pin the GraphQL contract: enum members match the ChoiceSets, storage_key stays exposed."""

    def test_source_type_enum_matches_choices(self):
        self.assertEqual(
            {member.value for member in ProjectSourceTypeEnum},
            set(ProjectSourceTypeChoices.values()),
        )

    def test_activation_policy_enum_matches_choices(self):
        self.assertEqual(
            {member.value for member in ActivationPolicyEnum},
            set(ActivationPolicyChoices.values()),
        )

    def test_storage_key_exposed_on_object_type(self):
        # storage_key is exposed read-only here as in REST, only filtering is withheld
        # (internal storage/runtime identity, see graphql/filters.py).
        field_names = {field.name for field in CustomScriptProjectType.__strawberry_definition__.fields}
        self.assertIn('storage_key', field_names)
