from netbox.api.serializers import PrimaryModelSerializer

from ...models import CustomScriptModule
from .project import CustomScriptProjectSerializer
from .revision import CustomScriptProjectRevisionSerializer


class CustomScriptModuleSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script Module model."""

    project = CustomScriptProjectSerializer(nested=True)
    last_discovered_revision = CustomScriptProjectRevisionSerializer(nested=True, read_only=True)

    class Meta:
        model = CustomScriptModule
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'project',
            'source_path',
            'enabled',
            'discovery_status',
            'discovery_error',
            'last_discovered_revision',
            'description',
            'owner',
            'comments',
            'tags',
            'custom_fields',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'source_path', 'description')
