import django_tables2 as tables

from netbox.tables import PrimaryModelTable, columns

from ..models import CustomScriptModule


class CustomScriptModuleTable(PrimaryModelTable):
    """Table for the Custom Script Module list view."""

    source_path = tables.Column(
        linkify=True,
    )
    project = tables.Column(
        linkify=True,
    )
    enabled = columns.BooleanColumn()
    discovery_status = columns.ChoiceFieldColumn()
    last_discovered_revision = tables.Column(linkify=True)
    tags = columns.TagColumn(
        url_name='plugins:netbox_scripts:customscriptmodule_list',
    )

    class Meta(PrimaryModelTable.Meta):
        model = CustomScriptModule
        fields = (
            'pk',
            'id',
            'source_path',
            'project',
            'enabled',
            'discovery_status',
            'discovery_error',
            'last_discovered_revision',
            'description',
            'comments',
            'owner',
            'owner_group',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = ('source_path', 'project', 'enabled', 'discovery_status')
