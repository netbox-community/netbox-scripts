from extras.ui.panels import CustomFieldsPanel, TagsPanel
from netbox.ui import layout
from netbox.ui.panels import CommentsPanel
from netbox.views import generic
from utilities.views import register_model_view

from ..models import CustomScript
from ..ui import CustomScriptPanel, CustomScriptStatePanel


@register_model_view(CustomScript)
class CustomScriptView(generic.ObjectView):
    """Detail view for a single Custom Script."""

    queryset = CustomScript.objects.all()
    # ObjectView defaults to clone, edit, and delete actions, and none of those routes exist
    # for a model whose rows only a revision activation may author.
    actions = ()
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
