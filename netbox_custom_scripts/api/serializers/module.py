from netbox.api.serializers import PrimaryModelSerializer

from ...models import CustomScriptModule
from .project import CustomScriptProjectSerializer


class CustomScriptModuleSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script Module model."""

    project = CustomScriptProjectSerializer(nested=True)

    class Meta:
        model = CustomScriptModule
        # last_discovered_revision stays a bare ID: revisions have no endpoint to nest.
        fields = (
            'id',
            'url',
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
