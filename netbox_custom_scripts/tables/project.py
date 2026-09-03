import django_tables2 as tables
from django.utils.translation import gettext_lazy as _

from netbox.tables import BaseTable, PrimaryModelTable, columns

from ..models import CustomScriptProject, CustomScriptProjectRevision


class CustomScriptProjectTable(PrimaryModelTable):
    """Table for the Custom Script Project list view."""

    name = tables.Column(
        linkify=True,
    )
    key = tables.Column()
    source_type = columns.ChoiceFieldColumn()
    data_source = tables.Column(
        linkify=True,
    )
    activation_policy = columns.ChoiceFieldColumn()
    enabled = columns.BooleanColumn()
    tags = columns.TagColumn(
        url_name='plugins:netbox_custom_scripts:customscriptproject_list',
    )

    class Meta(PrimaryModelTable.Meta):
        model = CustomScriptProject
        fields = (
            'pk',
            'id',
            'name',
            'key',
            'source_type',
            'data_source',
            'data_path',
            'activation_policy',
            'enabled',
            'description',
            'comments',
            'owner',
            'owner_group',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = ('name', 'key', 'source_type', 'data_source', 'enabled')


class CustomScriptProjectRevisionTable(BaseTable):
    """
    One project's revision history, for the project detail view.

    BaseTable rather than NetBoxTable, because a revision is history rather than an object a
    user edits: there is no list view to link to and no selection column. Implementation fields
    stay out, the manifest and the lease belong to a diagnostic view.

    The actions column carries no standard actions, only the two buttons that move the project
    between revisions. A revision has no edit, delete, or changelog route for the defaults to
    point at.
    """

    created = columns.DateTimeColumn(verbose_name=_('Created'))
    # Directly after the timestamp, because the linked column is a table's way into the detail
    # page and a revision is identified by its digest.
    short_digest = tables.Column(
        verbose_name=_('Digest'),
        accessor='short_digest',
        orderable=False,
        linkify=True,
        # A rejected staging has no digest, and its row is the one whose problems a reader most
        # wants, so the cell has to render rather than fall through to the empty default.
        empty_values=(),
    )
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    entrypoint_count = tables.Column(
        verbose_name=_('Entrypoints'),
        accessor='entrypoint_snapshot',
        # The accessor is the snapshot itself, so ordering would sort the JSON rather than
        # the length this column renders.
        orderable=False,
    )
    file_count = tables.Column(verbose_name=_('Files'))
    total_size = tables.Column(verbose_name=_('Size'))
    activated = columns.DateTimeColumn(verbose_name=_('Activated'))
    actions = columns.ActionsColumn(
        actions=(),
        extra_buttons="{% include 'netbox_custom_scripts/inc/revision_actions.html' %}",
    )

    # BaseTable hides every column a user has not selected, and column selection is not offered
    # for a table with no list view, so the actions column has to be exempt to render at all.
    # NetBoxTable exempts its own for the same reason.
    exempt_columns = ('actions',)

    class Meta(BaseTable.Meta):
        model = CustomScriptProjectRevision
        fields = ('created', 'short_digest', 'status', 'entrypoint_count', 'file_count', 'total_size', 'activated')
        default_columns = fields
        order_by = ('-created',)

    def render_short_digest(self, value):
        """Name a staging whose content was rejected before anything was stored."""
        return value or _('Not stored')

    def render_entrypoint_count(self, value):
        """Count the declared entrypoints, which is what two revisions on one digest differ on."""
        return len(value)


class CustomScriptProjectFileTable(BaseTable):
    """
    The files of a project's current revision, for the project's Files tab.

    Rows are the manifest's plain dictionaries plus one row per declared path the served
    revision does not hold, so this table is fed a list and has no queryset behind it.
    """

    path = tables.Column(
        verbose_name=_('Path'),
    )
    size = tables.Column(
        verbose_name=_('Size'),
        # Missing-path rows carry None, and ordering list data compares values, so a mixed column cannot sort.
        orderable=False,
    )
    sha256 = tables.Column(
        verbose_name=_('SHA256'),
        orderable=False,
    )
    entrypoint = columns.BooleanColumn(
        verbose_name=_('Entrypoint'),
    )

    class Meta(BaseTable.Meta):
        # ObjectChildrenView scopes saved table configurations by Meta.model, so the rows' source model stands in.
        model = CustomScriptProjectRevision
        empty_text = _('This project has no stored revision yet.')
        fields = ('path', 'size', 'sha256', 'entrypoint')
        default_columns = ('path', 'size', 'sha256', 'entrypoint')

    def render_path(self, value, record):
        """Annotate a declared path the served revision does not hold."""
        if record.get('awaiting'):
            return _('{path} (not in the active revision yet)').format(path=value)
        if record.get('missing'):
            return _('{path} (missing from the source)').format(path=value)
        return value

    def render_sha256(self, value):
        """Render the short digest form."""
        return value[:12]


class CustomScriptProjectRevisionEntrypointTable(BaseTable):
    """
    The entrypoints one revision froze, for its detail view.

    Rows are the entrypoint snapshot's own dictionaries, so this table is fed a list and has no
    queryset behind it. It is where the Revisions tab's entrypoint count resolves.
    """

    source_path = tables.Column(
        verbose_name=_('Source path'),
    )

    class Meta(BaseTable.Meta):
        # Required rather than decorative: a table with no model derives no default empty text.
        empty_text = _('This revision froze no entrypoints, so it publishes nothing.')
        fields = ('source_path',)
        default_columns = fields


class CustomScriptProjectRevisionProblemTable(BaseTable):
    """
    The problems one revision recorded, for its detail view.

    Rows are the normalized dictionaries the revision builds, so this table is fed a list and
    has no queryset behind it.
    """

    path = tables.Column(
        verbose_name=_('Path'),
        # An empty path is meaningful here, and a column renders its default instead of calling
        # the renderer for anything it counts as empty.
        empty_values=(),
    )
    code = tables.Column(
        verbose_name=_('Code'),
    )
    message = tables.Column(
        verbose_name=_('Message'),
    )
    # Off by default: it is the detail an import failure needs and noise on every other record.
    traceback = tables.Column(
        verbose_name=_('Traceback'),
    )

    class Meta(BaseTable.Meta):
        # Required rather than decorative: a table with no model derives no default empty text.
        empty_text = _('This revision recorded no problems.')
        fields = ('path', 'code', 'message', 'traceback')
        default_columns = ('path', 'code', 'message')

    def render_path(self, value):
        """Name the project itself for a problem no single file owns."""
        return value or _('The whole project')
