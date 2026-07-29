from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

_customscriptproject_item = PluginMenuItem(
    link='plugins:netbox_custom_scripts:customscriptproject_list',
    link_text=_('Custom Script Projects'),
    permissions=['netbox_custom_scripts.view_customscriptproject'],
    buttons=(
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptproject_add',
            title=_('Add'),
            icon_class='mdi mdi-plus-thick',
            permissions=['netbox_custom_scripts.add_customscriptproject'],
        ),
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptproject_bulk_import',
            title=_('Import'),
            icon_class='mdi mdi-upload',
            permissions=['netbox_custom_scripts.add_customscriptproject'],
        ),
    ),
)

_customscriptmodule_item = PluginMenuItem(
    link='plugins:netbox_custom_scripts:customscriptmodule_list',
    link_text=_('Custom Script Modules'),
    permissions=['netbox_custom_scripts.view_customscriptmodule'],
    buttons=(
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptmodule_add',
            title=_('Add'),
            icon_class='mdi mdi-plus-thick',
            permissions=['netbox_custom_scripts.add_customscriptmodule'],
        ),
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptmodule_bulk_import',
            title=_('Import'),
            icon_class='mdi mdi-upload',
            permissions=['netbox_custom_scripts.add_customscriptmodule'],
        ),
    ),
)

menu = PluginMenu(
    label=_('Custom Scripts'),
    groups=((_('Projects'), (_customscriptproject_item, _customscriptmodule_item)),),
    icon_class='mdi mdi-script-text',
)
