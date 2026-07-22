from django.utils.translation import gettext_lazy as _

from netbox.ui import attrs
from netbox.ui.panels import ObjectAttributesPanel


class CustomScriptProjectPanel(ObjectAttributesPanel):
    """Identity attributes of a Custom Script Project (detail view, left column)."""

    title = _('Project')

    name = attrs.TextAttr('name', label=_('Name'))
    key = attrs.TextAttr('key', label=_('Key'))
    storage_key = attrs.TextAttr('storage_key', label=_('Storage key'))
    enabled = attrs.BooleanAttr('enabled', label=_('Enabled'))
    description = attrs.TextAttr('description', label=_('Description'))


class CustomScriptProjectSourcePanel(ObjectAttributesPanel):
    """Source ownership and activation attributes (detail view, right column)."""

    title = _('Source')

    source_type = attrs.ChoiceAttr('source_type', label=_('Source type'))
    data_source = attrs.RelatedObjectAttr('data_source', label=_('Data source'))
    data_path = attrs.TextAttr('data_path', label=_('Data path'))
    activation_policy = attrs.ChoiceAttr('activation_policy', label=_('Activation policy'))
