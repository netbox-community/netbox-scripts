from core.api.serializers import DataSourceSerializer
from netbox.api.serializers import PrimaryModelSerializer

from ...models import CustomScriptProject
from ...validators import normalize_data_path


class CustomScriptProjectSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script Project model."""

    data_source = DataSourceSerializer(
        nested=True,
        required=False,
        allow_null=True,
        default=None,
    )

    def validate_data_path(self, value):
        """Return the data path in canonical form so the persisted value is never raw."""
        # Canonicalize here as well: model clean() normalizes only its instance copy,
        # while DRF persists validated_data, which would otherwise keep the raw spelling.
        return normalize_data_path(value)

    class Meta:
        model = CustomScriptProject
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'name',
            'key',
            'storage_key',
            'source_type',
            'data_source',
            'data_path',
            'activation_policy',
            'enabled',
            'description',
            'owner',
            'comments',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'key', 'description')
