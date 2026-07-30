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
    status = columns.ChoiceFieldColumn(verbose_name=_('Status'))
    short_digest = tables.Column(verbose_name=_('Digest'), accessor='short_digest', orderable=False)
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
        fields = ('created', 'status', 'short_digest', 'file_count', 'total_size', 'activated')
        default_columns = fields
        order_by = ('-created',)
