import django_tables2 as tables

from netbox.tables import PrimaryModelTable, columns

from ..models import CustomScript


class CustomScriptTable(PrimaryModelTable):
    """Table for the Custom Script list view. Retirement is a default column, not a filter."""

    display_name = tables.Column(
        linkify=True,
    )
    project = tables.Column(
        linkify=True,
    )
    enabled = columns.BooleanColumn()
    is_retired = columns.BooleanColumn()
    # Not linkified: revisions have no detail view.
    last_seen_revision = tables.Column()
    tags = columns.TagColumn(
        url_name='plugins:netbox_custom_scripts:customscript_list',
    )
    # Drops the default 'delete' item, which reverses a route this model does not register.
    # ActionsColumn resolves a URL for every action the viewer holds the permission for, so
    # the default renders fine for a narrowly permissioned user and raises for a superuser.
    actions = columns.ActionsColumn(
        actions=('edit', 'changelog'),
    )

    class Meta(PrimaryModelTable.Meta):
        model = CustomScript
        fields = (
            'pk',
            'id',
            'display_name',
            'project',
            'module_path',
            'class_name',
            'enabled',
            'is_retired',
            'last_seen_revision',
            'description',
            'comments',
            'owner',
            'owner_group',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = ('display_name', 'project', 'module_path', 'class_name', 'enabled', 'is_retired')
