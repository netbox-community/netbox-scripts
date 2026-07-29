from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel, ObjectsTablePanel
from netbox.views import generic
from utilities.views import register_model_view

from ..filtersets import CustomScriptProjectFilterSet
from ..forms import (
    CustomScriptProjectBulkEditForm,
    CustomScriptProjectBulkImportForm,
    CustomScriptProjectEditForm,
    CustomScriptProjectFilterForm,
)
from ..models import CustomScriptProject
from ..tables import CustomScriptProjectTable
from ..ui import CustomScriptProjectPanel, CustomScriptProjectSourcePanel


@register_model_view(CustomScriptProject, 'list', path='', detail=False)
class CustomScriptProjectListView(generic.ObjectListView):
    """List view for Custom Script Projects."""

    queryset = CustomScriptProject.objects.all()
    table = CustomScriptProjectTable
    filterset = CustomScriptProjectFilterSet
    filterset_form = CustomScriptProjectFilterForm


@register_model_view(CustomScriptProject)
class CustomScriptProjectView(generic.ObjectView):
    """Detail view for a single Custom Script Project."""

    queryset = CustomScriptProject.objects.all()
    layout = layout.SimpleLayout(
        left_panels=[
            CustomScriptProjectPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomScriptProjectSourcePanel(),
            CustomFieldsPanel(),
        ],
        bottom_panels=[
            ObjectsTablePanel(
                'netbox_custom_scripts.customscriptmodule',
                filters={'project_id': lambda context: context['object'].pk},
            ),
        ],
    )


@register_model_view(CustomScriptProject, 'add', detail=False)
@register_model_view(CustomScriptProject, 'edit')
class CustomScriptProjectEditView(generic.ObjectEditView):
    """Create and edit view for a Custom Script Project."""

    queryset = CustomScriptProject.objects.all()
    form = CustomScriptProjectEditForm


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
