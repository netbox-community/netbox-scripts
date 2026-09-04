from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ...choices import FileDiscoveryStatusChoices
from ...models import ScriptFile
from .project import ScriptProjectSerializer
from .revision import ScriptProjectRevisionSerializer


class ScriptFileSerializer(PrimaryModelSerializer):
    """Serializer for the Script File model."""

    project = ScriptProjectSerializer(nested=True)
    last_discovered_revision = ScriptProjectRevisionSerializer(nested=True, read_only=True)

    discovery_status = ChoiceField(choices=FileDiscoveryStatusChoices, read_only=True)

    class Meta:
        model = ScriptFile
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
