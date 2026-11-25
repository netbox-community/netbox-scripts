import logging
from contextlib import ExitStack, contextmanager
from pathlib import PurePosixPath

from django.core.exceptions import FieldError, ImproperlyConfigured, ValidationError
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from django.db import router, transaction
from django.utils.functional import cached_property
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as APIValidationError
from rest_framework.response import Response

from core.api.serializers import JobSerializer
from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet
from netbox.api.viewsets.mixins import discard_events_on_rollback
from utilities.exceptions import PermissionsViolation, RQWorkerNotRunningException
from utilities.forms import restrict_form_fields
from utilities.permissions import get_permission_for_model
from utilities.request import copy_safe_request
from utilities.rqworker import any_workers_for_queue, get_queue_for_model

from ..activation import activate_revision
from ..choices import ProjectSourceTypeChoices, RevisionStatusChoices
from ..constants import PENDING_VERDICT_REVISION_STATUSES
from ..execution import LOAD_FAILURES, script_class_context
from ..filtersets import (
    NetBoxScriptFilterSet,
    ScriptFileFilterSet,
    ScriptProjectFilterSet,
    ScriptProjectRevisionFilterSet,
)
from ..ingestion import (
    check_upload_preconditions,
    ingest_upload,
    prepare_upload,
    queue_revision_processing,
    script_file_edit,
    uploaded_source_path,
)
from ..jobs import NetBoxScriptJob, ProjectReconciliationJob, ProjectScriptFileRefreshJob
from ..models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from ..storage import config
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..storage.locks import project_lock
from ..storage.service import require_default_database
from .schemas import (
    PROJECT_ACTIVATION,
    SCRIPT_FILE_SELECTION,
    SCRIPT_FILE_STATE,
    NetBoxScriptRunMultipartInputSerializer,
)
from .serializers import (
    NetBoxScriptRunInputSerializer,
    NetBoxScriptSerializer,
    ScriptFileSerializer,
    ScriptProjectRevisionInputSerializer,
    ScriptProjectRevisionSerializer,
    ScriptProjectSerializer,
    ScriptProjectUploadSerializer,
)

logger = logging.getLogger('netbox.plugins.netbox_scripts.api')

#
# Permission classes
#


class ActivateRevisionPermissions(TokenPermissions):
    """Resolve a POST to the activate permission, which the method-derived default spells as add."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.activate_%(model_name)s']}


class ReconcileSourcePermissions(TokenPermissions):
    """Resolve a POST to the reconcile permission, which the method-derived default spells as add."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.reconcile_%(model_name)s']}


class RunScriptPermissions(TokenPermissions):
    """Resolve a POST to the run permission, which the method-derived default spells as add."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.run_%(model_name)s']}


class UploadSourcePermissions(TokenPermissions):
    """Resolve a POST to the change permission, since the project exists and its source moves."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.change_%(model_name)s']}


class ValidateRevisionPermissions(TokenPermissions):
    """Resolve a POST to the change permission, which the method-derived default spells as add."""

    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.change_%(model_name)s']}


#
# View mixins
#


class ProjectLockOrderMixin:
    """Take the owning Projects' locks before NetBox's row locks. Updates hold them past the commit or rollback."""

    # The field of this viewset's model that holds its Project's primary key.
    project_id_field = 'pk'

    def update(self, request, *args, **kwargs):
        """Lock the Project before NetBox locks and reads the row."""
        manager = self.queryset.model._default_manager
        lookup_url_kwarg = self.lookup_url_kwarg or self.lookup_field
        try:
            queryset = manager.filter(**{self.lookup_field: self.kwargs[lookup_url_kwarg]})
        except (KeyError, FieldError, TypeError, ValueError, ValidationError):
            # Locks nothing, and NetBox answers the lookup itself.
            queryset = manager.none()
        with self._project_locks(queryset):
            return super().update(request, *args, **kwargs)

    def perform_bulk_update(self, objects, update_data, partial):
        """Lock every Project in the batch before NetBox opens its transaction."""
        with self._project_locks(self.queryset.model._default_manager.filter(pk__in=update_data.keys())):
            return super().perform_bulk_update(objects, update_data, partial)

    def perform_destroy(self, instance):
        """Delete under the project lock."""
        with self._project_locks(self.queryset.model._default_manager.filter(pk=instance.pk)):
            super().perform_destroy(instance)

    @contextmanager
    def _project_locks(self, queryset):
        """Hold the locks of the Projects owning these rows, on the connection NetBox writes through."""
        using = router.db_for_write(queryset.model)
        storage_keys = (
            ScriptProject.objects.using(using)
            .filter(pk__in=queryset.values(self.project_id_field))
            # Name order, as bulk edit and delete take these locks. The pk makes it total.
            .order_by('name', 'pk')
            .values_list('storage_key', flat=True)
        )
        with ExitStack() as stack:
            for storage_key in storage_keys:
                stack.enter_context(project_lock(storage_key, using=using))
            yield


#
# Viewsets
#


class ScriptFileViewSet(ProjectLockOrderMixin, NetBoxModelViewSet):
    """REST API viewset for Script Files. Read and update only: POST and DELETE answer 405."""

    queryset = ScriptFile.objects.select_related('project', 'last_discovered_revision')
    serializer_class = ScriptFileSerializer
    filterset_class = ScriptFileFilterSet
    project_id_field = 'project_id'
    # A nested numeric id reaches any Project, so declarations are written on the Project only.
    # By method, not by mixin composition, for the reason on NetBoxScriptViewSet below.
    http_method_names = ('get', 'put', 'patch', 'head', 'options')

    @cached_property
    def queued_project_ids(self):
        """One set per request, so a bulk update queues each Project's refresh once."""
        return set()

    def perform_update(self, serializer):
        """Update one Script File under its Project's Script Files tab rules."""
        using = router.db_for_write(self.queryset.model)
        try:
            with (
                transaction.atomic(using=using),
                discard_events_on_rollback(self, using=using),
                script_file_edit(
                    serializer.instance, user=self.request.user, queued_project_ids=self.queued_project_ids
                ),
            ):
                super().perform_update(serializer)
        except PermissionsViolation as error:
            # Django's class, which NetBox's bulk update reports per entry and DRF's escapes.
            raise DjangoPermissionDenied(error.message) from error


class ScriptProjectViewSet(ProjectLockOrderMixin, NetBoxModelViewSet):
    """REST API viewset for Script Projects."""

    queryset = ScriptProject.objects.all()
    serializer_class = ScriptProjectSerializer
    filterset_class = ScriptProjectFilterSet
    # What each POST action is narrowed by, in place of the add permission NetBox reads from the method.
    action_permissions = {'activate': 'activate', 'reconcile': 'reconcile', 'upload': 'change', 'validate': 'change'}

    def initial(self, request, *args, **kwargs):
        """Narrow each Project action by its own permission rather than by its HTTP method."""
        super().initial(request, *args, **kwargs)
        if (permission := self.action_permissions.get(self.action)) and request.user.is_authenticated:
            # Re-derived from the class attribute, since the method-derived narrowing the base
            # class already applied restricts to what the user may add, which is the wrong side
            # of the pair for a project that already exists.
            self.queryset = type(self).queryset.restrict(request.user, permission)

    def get_bulk_destroy_queryset(self):
        """Return Projects in the lock-order mixin's order, which a bulk delete has to share."""
        return super().get_bulk_destroy_queryset().order_by('name', 'pk')

    @extend_schema(
        request={'multipart/form-data': ScriptProjectUploadSerializer},
        responses={
            201: OpenApiResponse(
                response=ScriptProjectRevisionSerializer,
                description=gettext_lazy(
                    'Returns the revision record for this upload. Inspect its status for the outcome.'
                ),
            ),
        },
    )
    @action(detail=True, methods=['post'], url_path='upload', permission_classes=[UploadSourcePermissions])
    def upload(self, request, pk=None):
        """Stage one uploaded Python file as a revision of this project's source."""
        project = self.get_object()
        # The upload declares its own script file, so it creates a Script File. The browser upload
        # asks for the same pair, and the two surfaces must not disagree about what it costs.
        if not request.user.has_perm('netbox_scripts.add_scriptfile'):
            raise PermissionDenied(_('Uploading source requires the Script File add permission.'))

        input_serializer = ScriptProjectUploadSerializer(data=request.data)
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
            using = check_upload_preconditions(project)
            with project_lock(project.storage_key, using=using):
                with transaction.atomic(using=using), discard_events_on_rollback(self, using=using):
                    prepare_upload(
                        project,
                        filename=path,
                        confirm_replace=input_serializer.validated_data['confirm_replace'],
                        user=request.user,
                    )
                staged = ingest_upload(project, filename=filename, content=upload.read(), declare=False)
        except PermissionsViolation as error:
            raise PermissionDenied(error.message) from error
        except ValidationError as error:
            raise APIValidationError({'file': error.messages}) from error
        except ImproperlyConfigured as error:
            logger.exception(
                'Could not accept an upload for Script Project %s due to a configuration error.', project.pk
            )
            raise APIValidationError(
                {'detail': _('The upload could not be accepted because of a configuration error.')}
            ) from error
        except (StorageError, OSError) as error:
            logger.exception('Could not store an upload for Script Project %s.', project.pk)
            raise APIValidationError({'detail': _('The upload could not be stored.')}) from error

        return Response(
            ScriptProjectRevisionSerializer(staged.revision, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        methods=['GET'],
        responses={200: OpenApiResponse(response=SCRIPT_FILE_STATE)},
    )
    @extend_schema(
        methods=['PUT'],
        request={'application/json': SCRIPT_FILE_SELECTION},
        responses={200: OpenApiResponse(response=SCRIPT_FILE_STATE)},
    )
    @action(detail=True, methods=['get', 'put'], url_path='script-files')
    def script_files(self, request, pk=None):
        """Report or replace which of a project's source modules are its script files."""
        # Token permissions already require the project's change permission for a PUT here.
        project = self.get_object()
        if request.method == 'PUT':
            if not request.user.has_perm('netbox_scripts.change_scriptfile'):
                raise PermissionDenied(_('Changing script files requires the Script File change permission.'))
            paths = request.data.get('paths')
            if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
                raise APIValidationError({'paths': _('Provide a list of source paths.')})
            using = require_default_database(project)
            try:
                with transaction.atomic(using=using), discard_events_on_rollback(self, using=using):
                    if project.select_script_files(paths, user=request.user):
                        ProjectScriptFileRefreshJob.enqueue_refresh(project)
            except PermissionsViolation as error:
                raise PermissionDenied(error.message) from error
            except ValidationError as error:
                raise APIValidationError(error.message_dict) from error
        return Response(self._script_file_state(project))

    @staticmethod
    def _script_file_state(project):
        """Return the candidate inventory annotated with what the project has declared."""
        declared = {script_file.source_path: script_file for script_file in project.script_files.all()}
        available = set(project.script_file_candidates())
        return {
            'mode': 'ui',
            'candidates': [
                {
                    'path': path,
                    'selected': path in declared and declared[path].enabled,
                    'available': path in available,
                    # Tab state, not a serializer representation, so this stays a bare value
                    # where the Script File serializer renders the value and label pair.
                    'discovery_status': declared[path].discovery_status if path in declared else None,
                }
                for path in project.declarable_script_files()
            ],
        }

    @extend_schema(
        request=None,
        responses={
            201: OpenApiResponse(
                response=JobSerializer,
                description=gettext_lazy(
                    'The reconciliation has been queued. Once it has staged the source, its data names the revision.'
                ),
            ),
        },
    )
    @action(detail=True, methods=['post'], url_path='reconcile', permission_classes=[ReconcileSourcePermissions])
    def reconcile(self, request, pk=None):
        """Queue a rebuild of this Project's source from its Data Source directory and return the Job."""
        project = self.get_object()
        # The browser route excludes these by its queryset, which over REST would hide why.
        if project.source_type != ProjectSourceTypeChoices.DATA_SOURCE:
            raise APIValidationError(
                {
                    'detail': _(
                        'Only projects backed by a Data Source can be reconciled. "{project}" holds uploaded files.'
                    ).format(project=project)
                }
            )
        job = ProjectReconciliationJob.enqueue_reconciliation(project)
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request={'application/json': ScriptProjectRevisionInputSerializer},
        responses={
            201: OpenApiResponse(
                response=JobSerializer,
                description=gettext_lazy('The revision has been queued for validation.'),
            ),
        },
    )
    @action(detail=True, methods=['post'], url_path='validate', permission_classes=[ValidateRevisionPermissions])
    def validate(self, request, pk=None):
        """Queue validation of one revision of this Project that awaits a verdict and return the Job."""
        project = self.get_object()
        input_serializer = ScriptProjectRevisionInputSerializer(data=request.data, context={'project': project})
        input_serializer.is_valid(raise_exception=True)
        revision = input_serializer.validated_data['revision']
        # For a revision holding a verdict, the same call queues automatic activation instead.
        job = queue_revision_processing(revision) if revision.status in PENDING_VERDICT_REVISION_STATUSES else None
        if job is None:
            message = _('Revision {revision} has status "{status}" and cannot be queued for validation.')
            return Response(
                {'detail': message.format(revision=revision.pk, status=revision.status)},
                status=status.HTTP_409_CONFLICT,
            )
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        request={'application/json': ScriptProjectRevisionInputSerializer},
        responses={
            200: OpenApiResponse(
                response=PROJECT_ACTIVATION,
                description=gettext_lazy('The revision is now the active revision of the Project.'),
            ),
        },
    )
    @action(detail=True, methods=['post'], url_path='activate', permission_classes=[ActivateRevisionPermissions])
    def activate(self, request, pk=None):
        """Make one valid or retired revision of this Project the active one and report its Scripts."""
        project = self.get_object()
        input_serializer = ScriptProjectRevisionInputSerializer(data=request.data, context={'project': project})
        input_serializer.is_valid(raise_exception=True)
        revision = input_serializer.validated_data['revision']
        using = require_default_database(project)
        with project_lock(project.storage_key, using=using):
            # Read under the lock, or a revision activated meanwhile takes the service's repair path.
            active_revision_id = (
                ScriptProject.objects.using(using)
                .filter(pk=project.pk)
                .values_list('active_revision_id', flat=True)
                .first()
            )
            if revision.pk == active_revision_id:
                message = _('Revision {revision} has status "{status}" and cannot be activated.')
                return Response(
                    {'detail': message.format(revision=revision.pk, status=RevisionStatusChoices.ACTIVE)},
                    status=status.HTTP_409_CONFLICT,
                )
            try:
                result = activate_revision(revision)
            except (ActivationError, RevisionCorruptError) as error:
                # Expected refusals: the revision cannot be activated, or its stored tree no longer matches.
                logger.warning(
                    'Revision %s of Script Project %s cannot be activated: %s', revision.pk, project.pk, error
                )
                return Response({'detail': _('The revision cannot be activated.')}, status=status.HTTP_409_CONFLICT)
            except (StorageError, OSError) as error:
                logger.exception('Could not activate revision %s of Script Project %s.', revision.pk, project.pk)
                raise APIValidationError({'detail': _('The revision could not be activated.')}) from error
        return Response(
            {
                'revision': ScriptProjectRevisionSerializer(result.revision, context={'request': request}).data,
                'scripts': {'published': result.scripts.published, 'retired': result.scripts.retired},
            }
        )


class ScriptProjectRevisionViewSet(NetBoxReadOnlyModelViewSet):
    """
    Read-only REST API viewset for Script Project Revisions.

    Revisions are produced by ingestion and moved through their lifecycle by the storage and
    validation services, so no write route is registered at all. Activation stays an action on
    the project rather than a writable status field.
    """

    queryset = ScriptProjectRevision.objects.select_related('project')
    serializer_class = ScriptProjectRevisionSerializer
    filterset_class = ScriptProjectRevisionFilterSet


class NetBoxScriptViewSet(NetBoxModelViewSet):
    """
    REST API viewset for Scripts.

    Update only, plus a run action. Rows are derived from an activated revision, so creation and
    deletion are refused and the serializer accepts the administrator's fields alone. Requesting
    a run is a POST to a detail route, which authors nothing.
    """

    queryset = NetBoxScript.objects.select_related('project', 'last_seen_revision')
    serializer_class = NetBoxScriptSerializer
    filterset_class = NetBoxScriptFilterSet
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

    def get_bulk_update_queryset(self):
        """Return Scripts in primary-key order to match synchronization."""
        return super().get_bulk_update_queryset().order_by('pk')

    # http_method_names is checked on every dispatch, so the action declares its own as an
    # initkwarg, which applies to this route alone and leaves POST refused on the list route.
    @extend_schema(
        request={
            'application/json': NetBoxScriptRunInputSerializer,
            'multipart/form-data': NetBoxScriptRunMultipartInputSerializer,
        },
        responses={
            201: OpenApiResponse(
                response=JobSerializer,
                description=gettext_lazy('The Script has been enqueued for execution.'),
            ),
        },
    )
    @action(
        detail=True,
        methods=['post'],
        url_path='run',
        permission_classes=[RunScriptPermissions],
        http_method_names=('post', 'options'),
    )
    def run(self, request, pk=None):
        """Enqueue one run of this Script and return the Job it created."""
        script = self.get_object()
        if not script.is_executable:
            raise APIValidationError(
                {'detail': _('This Script cannot be run. {reason}').format(reason=script.run_refusal_reason)}
            )
        # Checked before the source is loaded, so a run nothing can pick up does no storage I/O.
        queue_name = get_queue_for_model(NetBoxScript._meta.model_name)
        if not any_workers_for_queue(queue_name):
            raise RQWorkerNotRunningException()
        with ExitStack() as stack:
            try:
                script_class = stack.enter_context(script_class_context(script))
            except LOAD_FAILURES as error:
                logger.exception('Could not load Script %s from its source.', script.pk)
                raise APIValidationError({'detail': _('The Script could not be loaded from its source.')}) from error
            return self._enqueue_run(request, script, script_class(), queue_name=queue_name)

    def _enqueue_run(self, request, script, instance, *, queue_name):
        """Validate inputs and enqueue while the script's revision namespace is loaded."""
        # In request.data an upload wins over a same-named text part, and a FileVar may share a parameter's name.
        input_data = request.POST if request.FILES else request.data
        input_serializer = NetBoxScriptRunInputSerializer(data=input_data, context={'script_class': type(instance)})
        input_serializer.is_valid(raise_exception=True)
        parameters = input_serializer.validated_data
        # REST has no form to omit the fields from, so the value is refused instead. Read after
        # validation, where an interval with no start time is anchored.
        if (parameters.get('schedule_at') or parameters.get('interval')) and not request.user.has_perm(
            get_permission_for_model(NetBoxScript, 'schedule'), script
        ):
            raise PermissionDenied(_('Scheduling a Script requires the schedule permission.'))

        # The class's own form validates the declared variables, accepting only objects the user can view.
        form = instance.as_form(
            parameters['data'],
            # A FileVar's value cannot travel in the JSON envelope, so it arrives as its own part.
            files=request.FILES,
            commit_default=script.commit_default,
            notifications_default=script.notifications_default,
        )
        restrict_form_fields(form, request.user)
        if not form.is_valid():
            raise APIValidationError(form.errors)
        values = dict(form.cleaned_data)
        # Discarded, so the execution parameters never reach the script as variable values.
        for name in ('_commit', '_schedule_at', '_interval', '_notifications'):
            values.pop(name, None)

        try:
            job = NetBoxScriptJob.enqueue_run(
                script,
                data=values,
                # An absent optional field is left out of validated_data, so the Script's default stands.
                commit=parameters.get('commit', script.commit_default),
                schedule_at=parameters.get('schedule_at'),
                interval=parameters.get('interval'),
                notifications=parameters.get('notifications'),
                # The worker is another process, so the request has to be picklable and sanitized.
                request=copy_safe_request(request),
                user=request.user,
                queue_name=queue_name,
            )
        except ValidationError as error:
            raise APIValidationError({'detail': ' '.join(error.messages)}) from error
        return Response(JobSerializer(job, context={'request': request}).data, status=status.HTTP_201_CREATED)
