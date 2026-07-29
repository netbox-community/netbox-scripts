from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import CustomScriptModuleFilterSet, CustomScriptProjectFilterSet
from ..models import CustomScriptModule, CustomScriptProject
from .serializers import CustomScriptModuleSerializer, CustomScriptProjectSerializer


class CustomScriptModuleViewSet(NetBoxModelViewSet):
    """REST API viewset for Custom Script Modules."""

    queryset = CustomScriptModule.objects.select_related('project')
    serializer_class = CustomScriptModuleSerializer
    filterset_class = CustomScriptModuleFilterSet


class CustomScriptProjectViewSet(NetBoxModelViewSet):
    """REST API viewset for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    serializer_class = CustomScriptProjectSerializer
    filterset_class = CustomScriptProjectFilterSet
