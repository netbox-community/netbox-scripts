from core.choices import JobNotificationChoices
from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ...models import CustomScript
from .project import CustomScriptProjectSerializer
from .revision import CustomScriptProjectRevisionSerializer


class CustomScriptSerializer(PrimaryModelSerializer):
    """Serializer for the Custom Script model."""

    project = CustomScriptProjectSerializer(nested=True)
    last_seen_revision = CustomScriptProjectRevisionSerializer(nested=True, read_only=True)

    # allow_blank, because the column spends '' on inherit. ChoiceField then also coerces a
    # submitted null to '', so all three overrides clear the same way.
    notifications_default_override = ChoiceField(choices=JobNotificationChoices, allow_blank=True, required=False)

    class Meta:
        model = CustomScript
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
