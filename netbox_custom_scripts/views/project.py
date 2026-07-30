from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

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
from ..filtersets import CustomScriptProjectFilterSet
from ..forms import (
    CustomScriptProjectAddScriptForm,
    CustomScriptProjectBulkEditForm,
    CustomScriptProjectBulkImportForm,
    CustomScriptProjectEditForm,
    CustomScriptProjectEntrypointsForm,
    CustomScriptProjectFilterForm,
    CustomScriptProjectUploadForm,
)
from ..models import CustomScriptProject, CustomScriptProjectRevision
from ..object_actions import ActivateRevision, AddScript
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..tables import CustomScriptProjectRevisionTable, CustomScriptProjectTable
from ..ui import CustomScriptProjectPanel, CustomScriptProjectSourcePanel, CustomScriptProjectStatePanel


@register_model_view(CustomScriptProject, 'list', path='', detail=False)
class CustomScriptProjectListView(generic.ObjectListView):
    """List view for Custom Script Projects."""

    # The default set also includes rename, which this model does not register, and
    # ActionsMixin filters by permission alone rather than by route.
    actions = (AddObject, BulkImport, BulkExport, BulkEdit, BulkDelete)
    queryset = CustomScriptProject.objects.all()
    table = CustomScriptProjectTable
    filterset = CustomScriptProjectFilterSet
    filterset_form = CustomScriptProjectFilterForm


@register_model_view(CustomScriptProject)
class CustomScriptProjectView(generic.ObjectView):
    """Detail view for a single Custom Script Project."""

    queryset = CustomScriptProject.objects.all()
    # Workflow order: add source, then put it in service.
    actions = (AddScript, ActivateRevision, CloneObject, EditObject, DeleteObject)
    layout = layout.SimpleLayout(
        left_panels=[
            CustomScriptProjectPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomScriptProjectSourcePanel(),
            CustomScriptProjectStatePanel(),
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            ObjectsTablePanel(
                'netbox_custom_scripts.customscriptmodule',
                filters={'project_id': lambda context: context['object'].pk},
            ),
            # Unfiltered by retirement on purpose: hiding a retired script would make one that
            # stopped being published look deleted while its row and Job history are still there.
            ObjectsTablePanel(
                'netbox_custom_scripts.customscript',
                filters={'project_id': lambda context: context['object'].pk},
            ),
        ],
    )


@register_model_view(CustomScriptProject, 'activate', path='activate')
class CustomScriptProjectActivateView(generic.ObjectView):
    """
    Point a Custom Script Project at its newest validated revision.

    A project whose activation policy is manual reaches VALID and stops, so without this there
    is no way to put it in service. GET names the revision that would go live and POST performs
    it, which is the shape every other state change in NetBox takes.

    The service re-reads and re-verifies everything under the project lock, so this view chooses
    a candidate and reports the outcome rather than deciding anything itself.
    """

    queryset = CustomScriptProject.objects.all()
    template_name = 'netbox_custom_scripts/customscriptproject_activate.html'

    def get_required_permission(self):
        """Require the change permission: this moves the project's active pointer."""
        return get_permission_for_model(self.queryset.model, 'change')

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
            activation.activate_revision(candidate)
        except (ActivationError, RevisionCorruptError, StorageError, OSError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            # The project keeps serving whatever it served before.
            messages.error(request, _('The revision could not be activated: {error}').format(error=error))
            return redirect(project.get_absolute_url())
        messages.success(
            request,
            _('Revision {revision} is now the active revision.').format(revision=candidate.short_digest),
        )
        return redirect(project.get_absolute_url())


@register_model_view(CustomScriptProject, 'revisions', path='revisions')
class CustomScriptProjectRevisionsView(generic.ObjectChildrenView):
    """
    A Custom Script Project's revision history.

    Its own tab rather than a panel on the detail page: revisions are the project's history, so
    they are worth a page of their own and not worth the room on the page answering "what is
    this project serving now". No actions, because a revision is never edited or created by
    hand, and the ones that will act on it belong to the revision itself rather than the table.
    """

    queryset = CustomScriptProject.objects.all()
    child_model = CustomScriptProjectRevision
    table = CustomScriptProjectRevisionTable
    actions = ()
    tab = ViewTab(
        label=_('Revisions'),
        badge=lambda obj: obj.revisions.count(),
        weight=600,
    )

    def get_children(self, request, parent):
        """Return every revision of this project, newest first per the table's ordering."""
        return parent.revisions.all()


@register_model_view(CustomScriptProject, 'entrypoints', path='entrypoints')
class CustomScriptProjectEntrypointsView(generic.ObjectEditView):
    """Select a Custom Script Project's executable entrypoints from its own source."""

    queryset = CustomScriptProject.objects.all()
    form = CustomScriptProjectEntrypointsForm
    tab = ViewTab(
        label=_('Entrypoints'),
        badge=lambda obj: obj.modules.filter(enabled=True).count(),
        weight=500,
    )

    def has_permission(self):
        """Require the project's change permission, which restrict() needs, plus the Module's."""
        return super().has_permission() and self.request.user.has_perm(
            'netbox_custom_scripts.change_customscriptmodule'
        )


@register_model_view(CustomScriptProject, 'add', detail=False)
@register_model_view(CustomScriptProject, 'edit')
class CustomScriptProjectEditView(generic.ObjectEditView):
    """Create and edit view for a Custom Script Project."""

    queryset = CustomScriptProject.objects.all()
    form = CustomScriptProjectEditForm


@register_model_view(CustomScriptProject, 'upload', path='upload', detail=False)
class CustomScriptProjectUploadView(generic.ObjectEditView):
    """Create a Custom Script Project from one uploaded script."""

    queryset = CustomScriptProject.objects.all()
    form = CustomScriptProjectUploadForm

    def has_permission(self):
        """Require the Module add permission too, since the upload declares its own entrypoint."""
        return super().has_permission() and self.request.user.has_perm('netbox_custom_scripts.add_customscriptmodule')


@register_model_view(CustomScriptProject, 'add_script', path='upload')
class CustomScriptProjectAddScriptView(generic.ObjectEditView):
    """
    Add one more script to an existing Custom Script Project.

    Registered on the detail route, so the base view asks for the project's change permission
    rather than its add permission. That is the right side of the pair: the project exists and
    its source is being changed.
    """

    queryset = CustomScriptProject.objects.all()
    form = CustomScriptProjectAddScriptForm

    def has_permission(self):
        """Require the Module add permission too, since the upload declares its own entrypoint."""
        return super().has_permission() and self.request.user.has_perm('netbox_custom_scripts.add_customscriptmodule')


@register_model_view(CustomScriptProject, 'delete')
class CustomScriptProjectDeleteView(generic.ObjectDeleteView):
    """Delete view for a single Custom Script Project."""

    queryset = CustomScriptProject.objects.all()


@register_model_view(CustomScriptProject, 'bulk_edit', path='edit', detail=False)
class CustomScriptProjectBulkEditView(generic.BulkEditView):
    """Bulk edit view for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    filterset = CustomScriptProjectFilterSet
    table = CustomScriptProjectTable
    form = CustomScriptProjectBulkEditForm


@register_model_view(CustomScriptProject, 'bulk_delete', path='delete', detail=False)
class CustomScriptProjectBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    filterset = CustomScriptProjectFilterSet
    table = CustomScriptProjectTable


@register_model_view(CustomScriptProject, 'bulk_import', path='import', detail=False)
class CustomScriptProjectBulkImportView(generic.BulkImportView):
    """Bulk import view for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    model_form = CustomScriptProjectBulkImportForm
