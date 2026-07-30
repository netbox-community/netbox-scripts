from netbox.api.serializers import PrimaryModelSerializer

from ...models import CustomScript
from .project import CustomScriptProjectSerializer


class CustomScriptSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script model."""

    project = CustomScriptProjectSerializer(nested=True)

    class Meta:
        model = CustomScript
        # last_seen_revision stays a bare ID: revisions have no endpoint to nest.
        fields = (
            'id',
            'url',
            'display',
            'project',
            'module_path',
            'class_name',
            'display_name',
            'enabled',
            'is_retired',
            'last_seen_revision',
            'metadata',
            'description',
            'owner',
            'comments',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'module_path', 'class_name', 'display_name')
