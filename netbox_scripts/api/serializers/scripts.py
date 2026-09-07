from core.choices import JobNotificationChoices
from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ...choices import FileDiscoveryStatusChoices
from ...models import NetBoxScript, ScriptFile
from .projects import ScriptProjectSerializer
from .revisions import ScriptProjectRevisionSerializer


class NetBoxScriptSerializer(PrimaryModelSerializer):
    """Serializer for the Script model."""

    project = ScriptProjectSerializer(nested=True, read_only=True)
    last_seen_revision = ScriptProjectRevisionSerializer(nested=True, read_only=True)

    # allow_blank, because the column spends '' on inherit. ChoiceField then also coerces a
    # submitted null to '', so all three overrides clear the same way.
    notifications_default_override = ChoiceField(choices=JobNotificationChoices, allow_blank=True, required=False)

    class Meta:
        model = NetBoxScript
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'project',
            'module_path',
            'class_name',
            'display_name',
            'enabled',
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
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
        # Everything synchronization owns. Named explicitly rather than left to editable=False,
        # so opening a write path takes a deliberate edit here.
        read_only_fields = (
            'project',
            'module_path',
            'class_name',
            'display_name',
            'description',
            'is_retired',
            'last_seen_revision',
            'metadata',
        )


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
