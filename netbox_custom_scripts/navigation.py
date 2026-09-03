from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

_customscriptproject_item = PluginMenuItem(
    link='plugins:netbox_custom_scripts:customscriptproject_list',
    link_text=_('Projects'),
    permissions=['netbox_custom_scripts.view_customscriptproject'],
    buttons=(
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptproject_add',
            title=_('Add'),
            icon_class='mdi mdi-plus-thick',
            permissions=['netbox_custom_scripts.add_customscriptproject'],
        ),
        # The shortest path to a working Project: name it and hand it a script.
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptproject_upload',
            title=_('Upload Script'),
            icon_class='mdi mdi-file-upload-outline',
            # Both, because the upload declares an entrypoint and the view requires the Module
            # half too. A menu button offers no inert state, so a missing half hides it.
            permissions=[
                'netbox_custom_scripts.add_customscriptproject',
                'netbox_custom_scripts.add_customscriptmodule',
            ],
        ),
        PluginMenuButton(
            link='plugins:netbox_custom_scripts:customscriptproject_bulk_import',
            title=_('Import'),
            icon_class='mdi mdi-upload',
            permissions=['netbox_custom_scripts.add_customscriptproject'],
        ),
    ),
)

# In the Projects group rather than a group of its own: a pass produces Projects.
_migration_item = PluginMenuItem(
    link='plugins:netbox_custom_scripts:migration',
    link_text=_('Migration'),
    permissions=['netbox_custom_scripts.add_customscriptproject'],
)

# No add button: rows are derived from an activated revision, never authored.
_customscript_item = PluginMenuItem(
    link='plugins:netbox_custom_scripts:customscript_list',
    link_text=_('Scripts'),
    permissions=['netbox_custom_scripts.view_customscript'],
)

menu = PluginMenu(
    label=_('Custom Scripts'),
    groups=(
        (
            _('Projects'),
            (
                _customscriptproject_item,
                _migration_item,
            ),
        ),
        (
            _('Scripts'),
            (_customscript_item,),
        ),
    ),
    icon_class='mdi mdi-script-text',
)
