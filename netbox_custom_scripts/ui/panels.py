from django.utils.translation import gettext_lazy as _

from netbox.ui import attrs
from netbox.ui.panels import ObjectAttributesPanel


class CustomScriptPanel(ObjectAttributesPanel):
    """Identity attributes of a Custom Script (detail view, left column)."""

    title = _('Script')

    project = attrs.RelatedObjectAttr('project', label=_('Custom Script Project'))
    module_path = attrs.TextAttr('module_path', label=_('Module path'))
    class_name = attrs.TextAttr('class_name', label=_('Class name'))
    display_name = attrs.TextAttr('display_name', label=_('Display name'))
    description = attrs.TextAttr('description', label=_('Description'))
    enabled = attrs.BooleanAttr('enabled', label=_('Enabled'))


class CustomScriptStatePanel(ObjectAttributesPanel):
    """System-managed publication state of a Custom Script (detail view, right column)."""

    title = _('Publication')

    is_retired = attrs.BooleanAttr('is_retired', label=_('Retired'))
    last_seen_revision = attrs.RelatedObjectAttr('last_seen_revision', label=_('Last seen revision'))
    # One row per execution default rather than the metadata JSON, which rendered as a raw dict.
    # Each reads a model accessor, so the panel never reaches into the record itself.
    commit_default = attrs.BooleanAttr('commit_default', label=_('Commit by default'))
    job_timeout_display = attrs.TextAttr('job_timeout_display', label=_('Run timeout'))
    notifications_default = attrs.ChoiceAttr('notifications_default', label=_('Notifications'))
    scheduling_enabled = attrs.BooleanAttr('scheduling_enabled', label=_('Scheduling allowed'))


class CustomScriptProjectRevisionPanel(ObjectAttributesPanel):
    """Identity attributes of a revision (detail view, left column)."""

    title = _('Revision')

    project = attrs.RelatedObjectAttr('project', label=_('Custom Script Project'))
    digest = attrs.TextAttr('digest', label=_('Digest'))
    entrypoint_digest = attrs.TextAttr('entrypoint_digest', label=_('Entrypoint digest'))
    file_count = attrs.NumericAttr('file_count', label=_('Files'))
    total_size = attrs.NumericAttr('total_size', label=_('Size'))


class CustomScriptProjectRevisionStatePanel(ObjectAttributesPanel):
    """Lifecycle state of a revision (detail view, right column)."""

    title = _('State')

    status = attrs.ChoiceAttr('status', label=_('Status'))
    created = attrs.DateTimeAttr('created', label=_('Created'))
    activated = attrs.DateTimeAttr('activated', label=_('Activated'))


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
    last_discovered_revision = attrs.RelatedObjectAttr('last_discovered_revision', label=_('Last discovered revision'))
    discovery_error = attrs.TextAttr('discovery_error', label=_('Error'))


class MigrationRunPanel(ObjectAttributesPanel):
    """State of one migration off the built-in feature (detail view, left column)."""

    title = _('Migration')

    state = attrs.ChoiceAttr('state', label=_('State'))
    created = attrs.DateTimeAttr('created', label=_('Started'))
    cutover_started = attrs.DateTimeAttr('cutover_started', label=_('Cutover started'))
    completed = attrs.DateTimeAttr('completed', label=_('Completed'))
    user = attrs.RelatedObjectAttr('user', label=_('Started by'))


class MigrationRunVersionPanel(ObjectAttributesPanel):
    """The versions a migration ran against (detail view, right column)."""

    # Recorded because the supported reversal is restoring the database and the source storage
    # together with the versions that wrote them, so an operator needs to read them back.
    title = _('Versions')

    netbox_version = attrs.TextAttr('netbox_version', label=_('NetBox'))
    plugin_version = attrs.TextAttr('plugin_version', label=_('Plugin'))


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
    revision = attrs.RelatedObjectAttr('current_revision', label=_('Revision'), linkify=True)
    file_count = attrs.NumericAttr('current_revision.file_count', label=_('Files'))
    total_size = attrs.NumericAttr('current_revision.total_size', label=_('Size'))
    activated = attrs.DateTimeAttr('current_revision.activated', label=_('Activated'))
