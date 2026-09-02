from pathlib import PurePosixPath

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as APIValidationError
from rest_framework.response import Response

from core.api.serializers import JobSerializer
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from utilities.exceptions import RQWorkerNotRunningException
from utilities.permissions import get_permission_for_model
from utilities.request import copy_safe_request
from utilities.rqworker import any_workers_for_queue

from ..execution import LOAD_FAILURES, load_script_class
from ..filtersets import (
    CustomScriptFilterSet,
    CustomScriptModuleFilterSet,
    CustomScriptProjectFilterSet,
    CustomScriptProjectRevisionFilterSet,
)
from ..ingestion import (
    check_upload_conflicts,
    current_source_tree,
    ingest_upload,
    uploaded_source_path,
)
from ..jobs import CustomScriptJob, ProjectEntrypointRefreshJob
from ..models import CustomScript, CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from ..storage import config
from .serializers import (
    CustomScriptModuleSerializer,
    CustomScriptProjectRevisionSerializer,
    CustomScriptProjectSerializer,
    CustomScriptProjectUploadSerializer,
    CustomScriptRunInputSerializer,
    CustomScriptSerializer,
)


class RunScriptPermissions(TokenPermissions):
    """Resolve a POST to the run permission, which the method-derived default spells as add."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.run_%(model_name)s']}


class UploadSourcePermissions(TokenPermissions):
    """Resolve a POST to the change permission, since the project exists and its source moves."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.change_%(model_name)s']}


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

    def initial(self, request, *args, **kwargs):
        """Narrow the upload action by the change permission rather than by its HTTP method."""
        super().initial(request, *args, **kwargs)
        if self.action == 'upload' and request.user.is_authenticated:
            # Re-derived from the class attribute, since the method-derived narrowing the base
            # class already applied restricts to what the user may add, which is the wrong side
            # of the pair for a project that already exists.
            self.queryset = type(self).queryset.restrict(request.user, 'change')

    @action(detail=True, methods=['post'], url_path='upload', permission_classes=[UploadSourcePermissions])
    def upload(self, request, pk=None):
        """Stage one uploaded Python file as a new revision of this project's source."""
        project = self.get_object()
        # The upload declares its own entrypoint, so it creates a Module. The browser upload
        # asks for the same pair, and the two surfaces must not disagree about what it costs.
        if not request.user.has_perm('netbox_custom_scripts.add_customscriptmodule'):
            raise PermissionDenied('Uploading source requires the Custom Script Module add permission.')

        input_serializer = CustomScriptProjectUploadSerializer(data=request.data)
        input_serializer.is_valid(raise_exception=True)
        upload = input_serializer.validated_data['file']
        # Flattened here rather than relying on the parser. Django reduces a browser upload to
        # its basename, and this route states the same rule in the plugin's own code.
        filename = PurePosixPath(upload.name).name
        try:
            path = uploaded_source_path(filename)
            # Bounded before the read, so an oversized body is refused rather than pulled into
            # the web process for the manifest builder to reject afterwards.
            limit = config.get_storage_limits().max_file_size
            if upload.size > limit:
                raise ValidationError(
                    _('The file is larger than the {limit} byte limit for one source file.').format(limit=limit)
                )
            check_upload_conflicts(project, path, confirm_replace=input_serializer.validated_data['confirm_replace'])
            staged = ingest_upload(
                project, filename=filename, content=upload.read(), base_files=current_source_tree(project)
            )
        except ValidationError as error:
            raise APIValidationError({'file': error.messages}) from error

        return Response(
            CustomScriptProjectRevisionSerializer(staged.revision, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

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
                    # Tab state, not a serializer representation, so this stays a bare value
                    # where the Module serializer renders the value and label pair.
                    'discovery_status': declared[path].discovery_status if path in declared else None,
                }
                for path in project.declarable_entrypoints()
            ],
        }


class CustomScriptProjectRevisionViewSet(NetBoxReadOnlyModelViewSet):
    """
    Read-only REST API viewset for Custom Script Project Revisions.

    Revisions are produced by ingestion and moved through their lifecycle by the storage and
    validation services, so no write route is registered at all. Activation stays an action on
    the project rather than a writable status field.
    """

    queryset = CustomScriptProjectRevision.objects.select_related('project')
    serializer_class = CustomScriptProjectRevisionSerializer
    filterset_class = CustomScriptProjectRevisionFilterSet


class CustomScriptViewSet(NetBoxModelViewSet):
    """
    REST API viewset for Custom Scripts.

    Update only, plus a run action. Rows are derived from an activated revision, so creation and
    deletion are refused and the serializer accepts the administrator's fields alone. Requesting
    a run is a POST to a detail route, which authors nothing.
    """

    queryset = CustomScript.objects.select_related('project', 'last_seen_revision')
    serializer_class = CustomScriptSerializer
    filterset_class = CustomScriptFilterSet
    # Refuses creation and deletion by method. Composing the mixins instead, the way a
    # read-only viewset does, would drop NetBoxModelViewSet.update(), and with it the
    # changelog's pre-change snapshot and the If-Match check. PATCH and PUT on the list route
    # stay available, so an operator can enable or disable many scripts in one call.
    http_method_names = ('get', 'put', 'patch', 'head', 'options')

    def initial(self, request, *args, **kwargs):
        """Narrow the run action by the run permission rather than by its HTTP method."""
        super().initial(request, *args, **kwargs)
        if self.action == 'run' and request.user.is_authenticated:
            # Re-derived from the class attribute, since the method-derived narrowing the base
            # class already applied restricts to what the user may add, and that set is empty.
            self.queryset = type(self).queryset.restrict(request.user, 'run')

    # http_method_names is checked on every dispatch, so the action declares its own as an
    # initkwarg, which applies to this route alone and leaves POST refused on the list route.
    @action(
        detail=True,
        methods=['post'],
        url_path='run',
        permission_classes=[RunScriptPermissions],
        http_method_names=('post', 'options'),
    )
    def run(self, request, pk=None):
        """Enqueue one run of this Custom Script and return the Job it created."""
        script = self.get_object()
        if not script.is_executable:
            raise APIValidationError(
                {'detail': _('This Custom Script cannot be run. {reason}').format(reason=script.run_refusal_reason)}
            )
        # Checked before the source is loaded, so a run nothing can pick up does no storage I/O.
        if not any_workers_for_queue('default'):
            raise RQWorkerNotRunningException()
        try:
            instance = load_script_class(script)()
        except LOAD_FAILURES as error:
            raise APIValidationError(
                {'detail': f'The Custom Script could not be loaded from its source: {error}'}
            ) from error

        input_serializer = CustomScriptRunInputSerializer(data=request.data, context={'script_class': type(instance)})
        input_serializer.is_valid(raise_exception=True)
        parameters = input_serializer.validated_data
        # REST has no form to omit the fields from, so the value is refused instead. Read after
        # validation, where an interval with no start time is anchored.
        if (parameters.get('schedule_at') or parameters.get('interval')) and not request.user.has_perm(
            get_permission_for_model(CustomScript, 'schedule'), script
        ):
            raise PermissionDenied('Scheduling a Custom Script requires the schedule permission.')

        # The declared variables are the only authority on what is valid, so the class's own form
        # validates them.
        form = instance.as_form(
            parameters['data'],
            commit_default=script.commit_default,
            notifications_default=script.notifications_default,
        )
        if not form.is_valid():
            raise APIValidationError(form.errors)
        values = dict(form.cleaned_data)
        # Discarded, so the execution parameters never reach the script as variable values.
        for name in ('_commit', '_schedule_at', '_interval', '_notifications'):
            values.pop(name, None)

        job = CustomScriptJob.enqueue_run(
            script,
            data=values,
            # An absent optional field is left out of validated_data, so the class default stands.
            commit=parameters.get('commit', script.commit_default),
            schedule_at=parameters.get('schedule_at'),
            interval=parameters.get('interval'),
            notifications=parameters.get('notifications'),
            # The worker is another process, so the request has to be picklable and sanitized.
            request=copy_safe_request(request),
            user=request.user,
        )
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_201_CREATED)
