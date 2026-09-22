"""Payload declarations for the OpenAPI schema, for actions that assemble their own responses."""

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers

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
