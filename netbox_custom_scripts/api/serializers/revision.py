from netbox.api.serializers import ValidatedModelSerializer

from ...models import CustomScriptProjectRevision
from .project import CustomScriptProjectSerializer


class CustomScriptProjectRevisionSerializer(ValidatedModelSerializer):
    """
    Serializer for the Custom Script Project Revision model.

    Revisions have no endpoint of their own, so this exists for the one caller that resolves a
    serializer by model name rather than by route: event serialization, which a change-logged
    model needs whenever a request deletes one. Deleting a project cascades its revisions, so
    that path runs whether or not anything ever activates a revision in a request.

    url and display_url are therefore left out of the fields. Both reverse a detail route from
    the model, and a revision has none, so including them would trade a missing serializer for
    a failed reversal.
    """

    project = CustomScriptProjectSerializer(nested=True)

    class Meta:
        model = CustomScriptProjectRevision
        fields = (
            'id',
            'display',
            'project',
            'digest',
            'status',
            'manifest',
            'file_count',
            'total_size',
            'validation_errors',
            'discovered_scripts',
            'entrypoint_snapshot',
            'entrypoint_digest',
            'activated',
            'created',
            'last_updated',
        )
        brief_fields = ('id', 'display', 'digest', 'status')
