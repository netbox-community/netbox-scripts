from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.views import register_model_view

from ..filtersets import CustomScriptModuleFilterSet
from ..forms import CustomScriptModuleEditForm, CustomScriptModuleFilterForm
from ..models import CustomScriptModule
from ..tables import CustomScriptModuleTable
from ..ui import CustomScriptModuleDiscoveryPanel, CustomScriptModulePanel


@register_model_view(CustomScriptModule, 'list', path='', detail=False)
class CustomScriptModuleListView(generic.ObjectListView):
    """List view for Custom Script Modules."""

    queryset = CustomScriptModule.objects.all()
    table = CustomScriptModuleTable
    filterset = CustomScriptModuleFilterSet
    filterset_form = CustomScriptModuleFilterForm


@register_model_view(CustomScriptModule)
class CustomScriptModuleView(generic.ObjectView):
    """Detail view for a single Custom Script Module."""

    queryset = CustomScriptModule.objects.all()
    layout = layout.SimpleLayout(
        left_panels=[
            CustomScriptModulePanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomScriptModuleDiscoveryPanel(),
            CustomFieldsPanel(),
        ],
    )


@register_model_view(CustomScriptModule, 'add', detail=False)
@register_model_view(CustomScriptModule, 'edit')
class CustomScriptModuleEditView(generic.ObjectEditView):
    """Create and edit view for a Custom Script Module."""

    queryset = CustomScriptModule.objects.all()
    form = CustomScriptModuleEditForm


@register_model_view(CustomScriptModule, 'delete')
class CustomScriptModuleDeleteView(generic.ObjectDeleteView):
    """Delete view for a single Custom Script Module."""

    queryset = CustomScriptModule.objects.all()


@register_model_view(CustomScriptModule, 'bulk_delete', path='delete', detail=False)
class CustomScriptModuleBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for Custom Script Modules."""

    queryset = CustomScriptModule.objects.all()
    filterset = CustomScriptModuleFilterSet
    table = CustomScriptModuleTable
