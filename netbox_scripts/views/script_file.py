from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.object_actions import AddObject, BulkDelete, BulkExport
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.views import register_model_view

from ..filtersets import ScriptFileFilterSet
from ..forms import ScriptFileEditForm, ScriptFileFilterForm
from ..models import ScriptFile
from ..tables import ScriptFileTable
from ..ui import ScriptFileDiscoveryPanel, ScriptFilePanel


@register_model_view(ScriptFile, 'list', path='', detail=False)
class ScriptFileListView(generic.ObjectListView):
    """List view for Script Files."""

    # The default set includes import, bulk edit, and rename, none of which this model
    # registers, and ActionsMixin filters by permission alone rather than by route.
    actions = (AddObject, BulkExport, BulkDelete)
    queryset = ScriptFile.objects.select_related('project', 'last_discovered_revision')
    table = ScriptFileTable
    filterset = ScriptFileFilterSet
    filterset_form = ScriptFileFilterForm


@register_model_view(ScriptFile)
class ScriptFileView(generic.ObjectView):
    """Detail view for a single Script File."""

    queryset = ScriptFile.objects.select_related('project')
    layout = layout.SimpleLayout(
        left_panels=[
            ScriptFilePanel(),
            TagsPanel(),
            CommentsPanel(),
        ],
        right_panels=[
            ScriptFileDiscoveryPanel(),
            CustomFieldsPanel(),
        ],
    )


@register_model_view(ScriptFile, 'add', detail=False)
@register_model_view(ScriptFile, 'edit')
class ScriptFileEditView(generic.ObjectEditView):
    """Create and edit view for a Script File."""

    queryset = ScriptFile.objects.select_related('project')
    form = ScriptFileEditForm


@register_model_view(ScriptFile, 'delete')
class ScriptFileDeleteView(generic.ObjectDeleteView):
    """Delete view for a single Script File."""

    queryset = ScriptFile.objects.select_related('project')


@register_model_view(ScriptFile, 'bulk_delete', path='delete', detail=False)
class ScriptFileBulkDeleteView(generic.BulkDeleteView):
    """Bulk delete view for Script Files."""

    queryset = ScriptFile.objects.select_related('project')
    filterset = ScriptFileFilterSet
    table = ScriptFileTable
