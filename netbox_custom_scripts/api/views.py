from django.core.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as APIValidationError
from rest_framework.response import Response

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

    @action(detail=True, methods=['get', 'put'], url_path='entrypoints')
    def entrypoints(self, request, pk=None):
        """Report or replace which of a project's source modules are its entrypoints."""
        # Token permissions already require the project's change permission for a PUT here.
        project = self.get_object()
        if request.method == 'PUT':
            if not request.user.has_perm('netbox_custom_scripts.change_customscriptmodule'):
                raise PermissionDenied('Changing entrypoints requires the Custom Script Module change permission.')
            paths = request.data.get('paths')
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise APIValidationError({'paths': 'Provide a list of source paths.'})
            try:
                project.select_entrypoints(paths)
            except ValidationError as error:
                raise APIValidationError(error.message_dict) from error
        return Response(self._entrypoint_state(project))

    @staticmethod
    def _entrypoint_state(project):
        """Return the candidate inventory annotated with what the project has declared."""
        declared = {module.source_path: module for module in project.modules.all()}
        available = set(project.entrypoint_candidates())
        return {
            'mode': 'ui',
            'candidates': [
                {
                    'path': path,
                    'selected': path in declared and declared[path].enabled,
                    'available': path in available,
                    'discovery_status': declared[path].discovery_status if path in declared else None,
                }
                for path in project.declarable_entrypoints()
            ],
        }
