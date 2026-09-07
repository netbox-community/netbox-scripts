from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import (
    AddObject,
    BulkDelete,
    BulkEdit,
    BulkExport,
    BulkImport,
    CloneObject,
    DeleteObject,
    EditObject,
)
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel, ObjectsTablePanel
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import ViewTab, register_model_view

from .. import activation
from ..choices import ProjectSourceTypeChoices
from ..filtersets import ScriptProjectFilterSet
from ..forms import (
    ScriptProjectAddScriptForm,
    ScriptProjectBulkEditForm,
    ScriptProjectBulkImportForm,
    ScriptProjectEditForm,
    ScriptProjectFilterForm,
    ScriptProjectScriptFilesForm,
    ScriptProjectUploadForm,
)
from ..jobs import ProjectReconciliationJob
from ..models import ScriptProject, ScriptProjectRevision
from ..object_actions import ActivateRevision, AddScript, ReconcileSource, RepairScripts
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..tables import ScriptProjectFileTable, ScriptProjectRevisionTable, ScriptProjectTable
from ..ui import ScriptProjectPanel, ScriptProjectSourcePanel, ScriptProjectStatePanel
from .revisions import activation_message


@register_model_view(ScriptProject, 'list', path='', detail=False)
class ScriptProjectListView(generic.ObjectListView):
    """List view for Script Projects."""

    # The default set also includes rename, which this model does not register, and
    # ActionsMixin filters by permission alone rather than by route.
    actions = (AddObject, BulkImport, BulkExport, BulkEdit, BulkDelete)
    queryset = ScriptProject.objects.select_related('data_source')
    table = ScriptProjectTable
    filterset = ScriptProjectFilterSet
    filterset_form = ScriptProjectFilterForm


@register_model_view(ScriptProject)
class ScriptProjectView(generic.ObjectView):
    """Detail view for a single Script Project."""

    queryset = ScriptProject.objects.select_related('data_source')
    # Workflow order: add source, then put it in service. Only one of the first two ever renders,
    # each for the source type it belongs to.
    actions = (AddScript, ReconcileSource, ActivateRevision, RepairScripts, CloneObject, EditObject, DeleteObject)
    layout = layout.SimpleLayout(
        left_panels=[
            ScriptProjectPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            ScriptProjectSourcePanel(),
            ScriptProjectStatePanel(),
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            ObjectsTablePanel(
                'netbox_scripts.scriptfile',
                filters={'project_id': lambda context: context['object'].pk},
            ),
            # Unfiltered by retirement on purpose: hiding a retired script would make one that
            # stopped being published look deleted while its row and Job history are still there.
            ObjectsTablePanel(
                'netbox_scripts.netboxscript',
                filters={'project_id': lambda context: context['object'].pk},
            ),
        ],
    )


@register_model_view(ScriptProject, 'activate', path='activate')
class ScriptProjectActivateView(generic.ObjectView):
    """
    Point a Script Project at its newest validated revision.

    A project whose activation policy is manual reaches VALID and stops, so without this there
    is no way to put it in service. GET names the revision that would go live and POST performs
    it, which is the shape every other state change in NetBox takes.

    The service re-reads and re-verifies everything under the project lock, so this view chooses
    a candidate and reports the outcome rather than deciding anything itself.
    """

    queryset = ScriptProject.objects.select_related('data_source')
    template_name = 'netbox_scripts/scriptproject_activate.html'

    def get_required_permission(self):
        """Require the activate permission, granted separately from change."""
        return get_permission_for_model(self.queryset.model, 'activate')

    def get(self, request, **kwargs):
        """Show which revision would go live, or say that none can."""
        project = self.get_object(**kwargs)
        return render(
            request,
            self.template_name,
            {
                'object': project,
                'candidate': project.activatable_revision(),
                'return_url': project.get_absolute_url(),
            },
        )

    def post(self, request, **kwargs):
        """Activate the candidate, reporting a refusal rather than raising at the user."""
        project = self.get_object(**kwargs)
        candidate = project.activatable_revision()
        if candidate is None:
            messages.error(request, _('This Project has no validated revision to activate.'))
            return redirect(project.get_absolute_url())
        try:
            result = activation.activate_revision(candidate)
        except (ActivationError, RevisionCorruptError, StorageError, OSError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            # The project keeps serving whatever it served before.
            messages.error(request, _('The revision could not be activated: {error}').format(error=error))
            return redirect(project.get_absolute_url())
        messages.success(request, activation_message(candidate, result.scripts))
        return redirect(project.get_absolute_url())


@register_model_view(ScriptProject, 'repair', path='repair')
class ScriptProjectRepairView(generic.ObjectView):
    """
    Republish a Project's Custom Script rows from the revision it is already serving.

    A recovery action for rows that drifted from the snapshot they derive from, which the
    Activate routes cannot reach: both exclude the revision in force, so re-activating it has no
    operator route of its own. GET confirms and POST performs.

    Activation is the only path that derives rows under the project lock, so this re-activates
    rather than synchronizing rows directly. The queryset is narrowed to projects serving
    something, so the route does not apply to one that is not.
    """

    queryset = ScriptProject.objects.filter(active_revision__isnull=False).select_related('active_revision')
    template_name = 'netbox_scripts/scriptproject_repair.html'

    def get_required_permission(self):
        """Require the activate permission, granted separately from change."""
        return get_permission_for_model(self.queryset.model, 'activate')

    def get(self, request, **kwargs):
        """Confirm, naming the revision the rows would be rebuilt from."""
        project = self.get_object(**kwargs)
        return render(
            request,
            self.template_name,
            {'object': project, 'return_url': project.get_absolute_url()},
        )

    def post(self, request, **kwargs):
        """Republish, reporting how many rows moved rather than a bare success."""
        project = self.get_object(**kwargs)
        try:
            result = activation.activate_revision(project.active_revision)
        except (ActivationError, RevisionCorruptError, StorageError, OSError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            messages.error(request, _('The Custom Scripts could not be repaired: {error}').format(error=error))
            return redirect(project.get_absolute_url())
        if result.scripts.written:
            messages.success(
                request,
                ngettext(
                    'Repaired {count} Custom Script of {project}.',
                    'Repaired {count} Custom Scripts of {project}.',
                    result.scripts.written,
                ).format(count=result.scripts.written, project=project),
            )
        else:
            # The whole point of the action: a repair and a no-op must not look the same.
            messages.success(
                request,
                _('Every Custom Script of {project} already matched its revision, so nothing was written.').format(
                    project=project
                ),
            )
        return redirect(project.get_absolute_url())


@register_model_view(ScriptProject, 'reconcile', path='reconcile')
class ScriptProjectReconcileView(generic.ObjectView):
    """
    Rebuild a Data Source-backed Project's source from its directory as it stands now.

    A project created today has no source until its Data Source next synchronizes, which could be
    hours away, so this reconciles against the current file inventory rather than waiting. It
    deliberately does not drive the Data Source's own synchronization: that inventory is what a
    project's source is built from, and refreshing it is the Data Source's own operation.

    The work itself is a job, because reconciliation reads a directory whose size is unknown
    until it is read and then writes content. Activation and repair verify an already staged
    tree, bounded at staging time, which is why those stay in the request. GET confirms
    and POST enqueues, and the queryset is narrowed to Data Source-backed projects, so the route
    does not apply to a project whose source is uploaded.
    """

    queryset = ScriptProject.objects.filter(source_type=ProjectSourceTypeChoices.DATA_SOURCE).select_related(
        'data_source'
    )
    template_name = 'netbox_scripts/scriptproject_reconcile.html'

    def get_required_permission(self):
        """Require the reconcile permission, granted separately from change."""
        return get_permission_for_model(self.queryset.model, 'reconcile')

    def get(self, request, **kwargs):
        """Confirm, naming the directory the source would be rebuilt from."""
        project = self.get_object(**kwargs)
        return render(
            request,
            self.template_name,
            {'object': project, 'return_url': project.get_absolute_url()},
        )

    def post(self, request, **kwargs):
        """Enqueue the reconciliation and say that it is under way."""
        project = self.get_object(**kwargs)
        ProjectReconciliationJob.enqueue_reconciliation(project)
        messages.success(
            request,
            _('Reconciling the source of {project} from {source}.').format(project=project, source=project.data_source),
        )
        return redirect(project.get_absolute_url())


@register_model_view(ScriptProject, 'revisions', path='revisions')
class ScriptProjectRevisionsView(generic.ObjectChildrenView):
    """
    A Script Project's revision history.

    Its own tab rather than a panel on the detail page: revisions are the project's history, so
    they are worth a page of their own and not worth the room on the page answering "what is
    this project serving now". No actions, because a revision is never edited or created by
    hand, and the ones that will act on it belong to the revision itself rather than the table.
    """

    queryset = ScriptProject.objects.select_related('data_source')
    child_model = ScriptProjectRevision
    table = ScriptProjectRevisionTable
    actions = ()
    tab = ViewTab(
        label=_('Revisions'),
        badge=lambda obj: obj.revisions.count(),
        # The badge callable never receives the request, so the tab carries the permission and
        # core skips rendering it entirely rather than showing a count over an empty table.
        permission='netbox_scripts.view_scriptprojectrevision',
        weight=600,
    )

    def get_children(self, request, parent):
        """Return every revision of this project the caller may view, newest first."""
        return parent.revisions.restrict(request.user, 'view')


@register_model_view(ScriptProject, 'script_files', path='script-files')
class ScriptProjectScriptFilesView(generic.ObjectEditView):
    """Select a Script Project's executable script files from its own source."""

    queryset = ScriptProject.objects.select_related('data_source')
    form = ScriptProjectScriptFilesForm
    tab = ViewTab(
        label=_('Script Files'),
        badge=lambda obj: obj.script_files.filter(enabled=True).count(),
        weight=500,
    )

    # The project's change permission is what restrict() needs, and the selection writes Script Files.
    additional_permissions = ('netbox_scripts.change_scriptfile',)


@register_model_view(ScriptProject, 'files', path='files')
class ScriptProjectFilesView(generic.ObjectChildrenView):
    """
    The files a Script Project's current revision holds.

    Read-only rows out of the revision's manifest, so no revision means an empty tab. The live
    declarations supply the script file marker and any declared path the source no longer holds.
    """

    queryset = ScriptProject.objects.select_related('active_revision')
    table = ScriptProjectFileTable
    actions = ()
    tab = ViewTab(
        label=_('Files'),
        badge=lambda obj: obj.current_revision.file_count if obj.current_revision else 0,
        permission='netbox_scripts.view_scriptprojectrevision',
        weight=550,
    )

    def get_children(self, request, parent):
        """Return one row per manifest entry, plus one per declared path absent from it."""
        revision = parent.current_revision
        # Rows are manifest dictionaries rather than a queryset, so the whole tab is gated on the
        # revision they came from instead of restricting what get_children returns.
        if (
            revision is None
            or not ScriptProjectRevision.objects.restrict(request.user, 'view').filter(pk=revision.pk).exists()
        ):
            return []
        declared = {script_file.source_path: script_file.enabled for script_file in parent.script_files.all()}
        present = {entry['path'] for entry in revision.manifest}
        awaiting = parent.paths_awaiting_activation()
        rows = [{**entry, 'script_file': declared.get(entry['path'], False)} for entry in revision.manifest]
        rows += [
            {
                'path': path,
                'size': None,
                'sha256': None,
                'script_file': enabled,
                'missing': True,
                'awaiting': path in awaiting,
            }
            for path, enabled in declared.items()
            if path not in present
        ]
        return sorted(rows, key=lambda row: row['path'])


@register_model_view(ScriptProject, 'add', detail=False)
@register_model_view(ScriptProject, 'edit')
class ScriptProjectEditView(generic.ObjectEditView):
    """Create and edit view for a Script Project."""

    queryset = ScriptProject.objects.select_related('data_source')
    form = ScriptProjectEditForm


@register_model_view(ScriptProject, 'upload', path='upload', detail=False)
class ScriptProjectUploadView(generic.ObjectEditView):
    """Create a Script Project from one uploaded script."""

    queryset = ScriptProject.objects.select_related('data_source')
    form = ScriptProjectUploadForm

    # The upload declares its own script file, so it creates a Script File.
    additional_permissions = ('netbox_scripts.add_scriptfile',)


@register_model_view(ScriptProject, 'add_script', path='upload')
class ScriptProjectAddScriptView(generic.ObjectEditView):
    """
    Add one more script to an existing Script Project.

    Registered on the detail route, so the base view asks for the project's change permission
    rather than its add permission. That is the right side of the pair: the project exists and
    its source is being changed.
    """

    queryset = ScriptProject.objects.select_related('data_source')
    form = ScriptProjectAddScriptForm

    # The upload declares its own script file, so it creates a Script File.
    additional_permissions = ('netbox_scripts.add_scriptfile',)


@register_model_view(ScriptProject, 'delete')
class ScriptProjectDeleteView(generic.ObjectDeleteView):
    """Delete view for a single Script Project."""

    queryset = ScriptProject.objects.select_related('data_source')


@register_model_view(ScriptProject, 'bulk_edit', path='edit', detail=False)
class ScriptProjectBulkEditView(generic.BulkEditView):
    """Bulk edit view for Script Projects."""

    queryset = ScriptProject.objects.select_related('data_source')
    filterset = ScriptProjectFilterSet
    table = ScriptProjectTable
    form = ScriptProjectBulkEditForm


@register_model_view(ScriptProject, 'bulk_delete', path='delete', detail=False)
class ScriptProjectBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for Script Projects."""

    queryset = ScriptProject.objects.select_related('data_source')
    filterset = ScriptProjectFilterSet
    table = ScriptProjectTable


@register_model_view(ScriptProject, 'bulk_import', path='import', detail=False)
class ScriptProjectBulkImportView(generic.BulkImportView):
    """Bulk import view for Script Projects."""

    queryset = ScriptProject.objects.select_related('data_source')
    model_form = ScriptProjectBulkImportForm
