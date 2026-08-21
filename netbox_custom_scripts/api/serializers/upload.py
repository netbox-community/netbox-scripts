from rest_framework import serializers


class CustomScriptProjectUploadSerializer(serializers.Serializer):
    """
    One uploaded Python file plus the confirmation a replacement needs.

    Accepts no destination. The path within the project is the file name's basename, matching
    what a browser upload produces.
    """

    file = serializers.FileField()
    confirm_replace = serializers.BooleanField(default=False)
