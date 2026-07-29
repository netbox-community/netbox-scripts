from django.utils.translation import gettext_lazy as _

from netbox.ui import attrs
from netbox.ui.panels import ObjectAttributesPanel


class CustomScriptModulePanel(ObjectAttributesPanel):
    """Declaration attributes of a Custom Script Module (detail view, left column)."""

    title = _('Module')

    project = attrs.RelatedObjectAttr('project', label=_('Custom Script Project'))
    source_path = attrs.TextAttr('source_path', label=_('Source path'))
    enabled = attrs.BooleanAttr('enabled', label=_('Enabled'))
    description = attrs.TextAttr('description', label=_('Description'))


class CustomScriptModuleDiscoveryPanel(ObjectAttributesPanel):
    """System-managed discovery results of a Custom Script Module (detail view, right column)."""

    title = _('Discovery')

    discovery_status = attrs.ChoiceAttr('discovery_status', label=_('Status'))
    # Not a RelatedObjectAttr: revisions have no detail view.
    last_discovered_revision = attrs.TextAttr('last_discovered_revision', label=_('Last discovered revision'))
    discovery_error = attrs.TextAttr('discovery_error', label=_('Error'))


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


class CustomScriptProjectStatePanel(ObjectAttributesPanel):
    """
    The revision a project is serving right now (detail view, right column).

    Answers the two questions an operator has after adding source: is it live, and if not what
    is it waiting on. The state line covers the second, the fields describe the tree currently
    in force. Earlier revisions are the Revisions tab's business, and the manifest, digests, and
    lease owner stay out of both.
    """

    title = _('Current revision')

    source_state = attrs.TextAttr('source_state', label=_('State'))
    created = attrs.DateTimeAttr('current_revision.created', label=_('Created'))
    status = attrs.ChoiceAttr('current_revision.status', label=_('Status'))
    short_digest = attrs.TextAttr('current_revision.short_digest', label=_('Digest'))
    file_count = attrs.NumericAttr('current_revision.file_count', label=_('Files'))
    total_size = attrs.NumericAttr('current_revision.total_size', label=_('Size'))
    activated = attrs.DateTimeAttr('current_revision.activated', label=_('Activated'))
