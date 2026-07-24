from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import CustomScriptProjectFilterSet
from ..models import CustomScriptProject
from .serializers import CustomScriptProjectSerializer


class CustomScriptProjectViewSet(NetBoxModelViewSet):
    """REST API viewset for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    serializer_class = CustomScriptProjectSerializer
    filterset_class = CustomScriptProjectFilterSet
