"""Payload declarations for the OpenAPI schema, for shapes no runtime serializer describes."""

from django.utils.translation import gettext_lazy as _
from drf_spectacular.extensions import OpenApiSerializerExtension
from drf_spectacular.utils import inline_serializer
from rest_framework import serializers

from .serializers import NetBoxScriptRunInputSerializer, ScriptProjectRevisionSerializer

# The action builds this mapping by hand, so there is no serializer for the schema to point at.
SCRIPT_FILE_STATE = inline_serializer(
    name='ScriptProjectScriptFiles',
    fields={
        'mode': serializers.CharField(),
        'candidates': inline_serializer(
            name='ScriptProjectScriptFileCandidate',
            fields={
                'path': serializers.CharField(),
                'selected': serializers.BooleanField(),
                'available': serializers.BooleanField(),
                'discovery_status': serializers.CharField(allow_null=True),
            },
            many=True,
        ),
    },
)

# The generator appends 'Request' to a request component, so the name must not already end in it.
SCRIPT_FILE_SELECTION = inline_serializer(
    name='ScriptProjectScriptFileSelection',
    fields={'paths': serializers.ListField(child=serializers.CharField())},
)

# The action builds this mapping around the revision serializer's representation.
PROJECT_ACTIVATION = inline_serializer(
    name='ScriptProjectActivation',
    fields={
        'revision': ScriptProjectRevisionSerializer(),
        'scripts': inline_serializer(
            name='ScriptProjectActivationScripts',
            fields={
                'published': serializers.IntegerField(help_text=_('Scripts the revision publishes.')),
                'retired': serializers.IntegerField(help_text=_('Scripts of the previous revision it retired.')),
            },
        ),
    },
)


class NetBoxScriptRunMultipartInputSerializer(NetBoxScriptRunInputSerializer):
    """A run request as multipart form data, which a Script with a FileVar variable needs."""

    data = serializers.CharField(
        required=False,
        help_text=_(
            "The Script's variable values as a JSON object encoded as a string. "
            'Send each file in a separate part named after its FileVar variable.'
        ),
    )


class NetBoxScriptRunMultipartInputSerializerExtension(OpenApiSerializerExtension):
    """Declares the multipart run request's file parts, each named after one of the Script's FileVar variables."""

    target_class = NetBoxScriptRunMultipartInputSerializer

    def map_serializer(self, auto_schema, direction):
        """Allow a binary part under every name the declared fields leave free."""
        schema = super().map_serializer(auto_schema, direction)
        schema['additionalProperties'] = {
            'type': 'string',
            'format': 'binary',
            'description': _('A file, in a part named after its FileVar variable.'),
        }
        return schema
