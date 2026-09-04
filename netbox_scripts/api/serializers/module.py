from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ...choices import ModuleDiscoveryStatusChoices
from ...models import CustomScriptModule
from .project import CustomScriptProjectSerializer
from .revision import ScriptProjectRevisionSerializer


class CustomScriptModuleSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script Module model."""

    project = CustomScriptProjectSerializer(nested=True)
    last_discovered_revision = ScriptProjectRevisionSerializer(nested=True, read_only=True)

    discovery_status = ChoiceField(choices=ModuleDiscoveryStatusChoices, read_only=True)

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
