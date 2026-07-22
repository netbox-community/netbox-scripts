import django_tables2 as tables

from netbox.tables import PrimaryModelTable, columns

from ..models import CustomScriptProject


class CustomScriptProjectTable(PrimaryModelTable):
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
