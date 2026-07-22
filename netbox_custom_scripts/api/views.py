from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import CustomScriptProjectFilterSet
from ..models import CustomScriptProject
from .serializers import CustomScriptProjectSerializer


class CustomScriptProjectViewSet(NetBoxModelViewSet):
    queryset = CustomScriptProject.objects.all()
    serializer_class = CustomScriptProjectSerializer
    filterset_class = CustomScriptProjectFilterSet
