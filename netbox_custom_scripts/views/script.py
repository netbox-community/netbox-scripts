from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import BulkEdit, BulkExport, EditObject
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.views import register_model_view

from ..filtersets import CustomScriptFilterSet
from ..forms import CustomScriptBulkEditForm, CustomScriptEditForm, CustomScriptFilterForm
from ..models import CustomScript
from ..tables import CustomScriptTable
from ..ui import CustomScriptPanel, CustomScriptStatePanel


@register_model_view(CustomScript, 'list', path='', detail=False)
class CustomScriptListView(generic.ObjectListView):
    """List view for Custom Scripts, retired ones included."""

    # ObjectListView defaults to add, import, export, bulk edit, rename, and delete, and
    # ActionsMixin filters those by permission alone, never by whether the route exists. Rows
    # are derived, so only these two have one.
    actions = (BulkExport, BulkEdit)
    # select_related is load bearing: the table linkifies project, so without it the list
    # issues one query per row.
    queryset = CustomScript.objects.select_related('project')
    table = CustomScriptTable
    filterset = CustomScriptFilterSet
    filterset_form = CustomScriptFilterForm


@register_model_view(CustomScript)
class CustomScriptView(generic.ObjectView):
    """Detail view for a single Custom Script."""

    queryset = CustomScript.objects.all()
    # No clone or delete: rows are derived from an activated revision, so the only authored
    # fields are the administrator's.
    actions = (EditObject,)
    layout = layout.SimpleLayout(
        left_panels=[
            CustomScriptPanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            CustomScriptStatePanel(),
            CustomFieldsPanel(),
        ],
    )


# No 'add' route is registered, which keeps the surface honest and also settles the
# add-versus-change permission question: an edit route always has a pk, so ObjectEditView
# requires the change permission.
@register_model_view(CustomScript, 'edit')
class CustomScriptEditView(generic.ObjectEditView):
    """Edit view for the administrator-owned fields of a Custom Script."""

    queryset = CustomScript.objects.all()
    form = CustomScriptEditForm


@register_model_view(CustomScript, 'bulk_edit', path='edit', detail=False)
class CustomScriptBulkEditView(generic.BulkEditView):
    """Bulk edit view for Custom Scripts, so enabled can be set across many rows."""

    queryset = CustomScript.objects.select_related('project')
    filterset = CustomScriptFilterSet
    table = CustomScriptTable
    form = CustomScriptBulkEditForm
