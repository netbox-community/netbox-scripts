from django.core.exceptions import ValidationError
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as APIValidationError
from rest_framework.response import Response

from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import (
    CustomScriptFilterSet,
    CustomScriptModuleFilterSet,
    CustomScriptProjectFilterSet,
    CustomScriptProjectRevisionFilterSet,
)
from ..jobs import ProjectEntrypointRefreshJob
from ..models import CustomScript, CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from .serializers import (
    CustomScriptModuleSerializer,
    CustomScriptProjectRevisionSerializer,
    CustomScriptProjectSerializer,
    CustomScriptSerializer,
)


class CustomScriptModuleViewSet(NetBoxModelViewSet):
    """REST API viewset for Custom Script Modules."""

    queryset = CustomScriptModule.objects.select_related('project', 'last_discovered_revision')
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
            selected = set(project.modules.filter(enabled=True).values_list('source_path', flat=True))
            try:
                project.select_entrypoints(paths)
            except ValidationError as error:
                raise APIValidationError(error.message_dict) from error
            if set(paths) != selected:
                # The same rule the Entrypoints tab follows, so the two surfaces cannot disagree
                # about what saving a selection does.
                ProjectEntrypointRefreshJob.enqueue_refresh(project)
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


class CustomScriptProjectRevisionViewSet(NetBoxModelViewSet):
    """
    Read-only REST API viewset for Custom Script Project Revisions.

    Revisions are produced by ingestion and moved through their lifecycle by the storage and
    validation services, so every write method is refused at the router. Activation stays an
    action on the project rather than a writable status field.
    """

    queryset = CustomScriptProjectRevision.objects.select_related('project')
    serializer_class = CustomScriptProjectRevisionSerializer
    filterset_class = CustomScriptProjectRevisionFilterSet
    http_method_names = ('get', 'head', 'options', 'trace')


class CustomScriptViewSet(NetBoxModelViewSet):
    """
    REST API viewset for Custom Scripts.

    Update only. Rows are derived from an activated revision, so POST and DELETE are refused
    and the serializer accepts the administrator's fields alone.
    """

    queryset = CustomScript.objects.select_related('project', 'last_seen_revision')
    serializer_class = CustomScriptSerializer
    filterset_class = CustomScriptFilterSet
    # Refuses creation and deletion at the router. PATCH and PUT on the list route stay
    # available, so an operator can enable or disable many scripts in one call.
    http_method_names = ('get', 'put', 'patch', 'head', 'options', 'trace')
