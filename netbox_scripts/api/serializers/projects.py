from rest_framework import serializers

from core.api.serializers import DataSourceSerializer
from netbox.api.fields import ChoiceField
from netbox.api.serializers import PrimaryModelSerializer

from ...choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ...models import ScriptProject
from ...permissions import ACTIVATE_PERMISSION, SOURCE_REFUSAL, moved_source_fields
from ...validators import normalize_data_path


class ScriptProjectSerializer(PrimaryModelSerializer):
    """Serializer for the Script Project model."""

    data_source = DataSourceSerializer(
        nested=True,
        required=False,
        allow_null=True,
        default=None,
    )

    # required=False on both, because each column carries a model default that an explicit
    # declaration would otherwise discard.
    source_type = ChoiceField(choices=ProjectSourceTypeChoices, required=False)
    activation_policy = ChoiceField(choices=ActivationPolicyChoices, required=False)

    def validate_data_path(self, value):
        """Return the data path in canonical form so the persisted value is never raw."""
        # Canonicalize here as well: model clean() normalizes only its instance copy,
        # while DRF persists validated_data, which would otherwise keep the raw spelling.
        return normalize_data_path(value)

    def validate(self, attrs):
        """Refuse a source-field move by a caller holding change but not activate."""
        attrs = super().validate(attrs)
        request = self.context.get('request')
        # No request means no browser or token write. Event serialization builds this without one.
        if self.instance is None or request is None or request.user.has_perm(ACTIVATE_PERMISSION):
            return attrs
        if moved := moved_source_fields(self.instance.pk, attrs):
            raise serializers.ValidationError(dict.fromkeys(moved, SOURCE_REFUSAL))
        return attrs

    class Meta:
        model = ScriptProject
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
