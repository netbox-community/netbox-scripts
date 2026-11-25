from rest_framework import serializers

from netbox.api.fields import ChoiceField
from netbox.api.serializers import ValidatedModelSerializer

from ...choices import RevisionStatusChoices
from ...models import ScriptProjectRevision
from .projects import ScriptProjectSerializer


class ScriptProjectRevisionSerializer(ValidatedModelSerializer):
    """
    Serializer for the Script Project Revision model.

    Carries a revision's identity, its position in the lifecycle, and the outcome of the
    validation that judged it. It also serves the caller that resolves a serializer by model
    name rather than by route, which is event serialization on a cascade delete.
    """

    project = ScriptProjectSerializer(nested=True)
    status = ChoiceField(choices=RevisionStatusChoices, read_only=True)

    class Meta:
        model = ScriptProjectRevision
        fields = (
            'id',
            'url',
            'display_url',
            'display',
            'project',
            'digest',
            'status',
            'file_count',
            'total_size',
            'validation_errors',
            'last_validation_failure',
            'discovered_scripts',
            'script_file_digest',
            'activated',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'digest', 'status')


class ScriptProjectRevisionInputSerializer(serializers.Serializer):
    """One revision of the Project passed as the context's project."""

    revision = serializers.PrimaryKeyRelatedField(queryset=ScriptProjectRevision.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # The schema generator builds this without a Project, and the field then only declares its type.
        if (project := self.context.get('project')) is not None:
            self.fields['revision'].queryset = project.revisions.all()
