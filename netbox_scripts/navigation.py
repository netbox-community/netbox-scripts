from django.utils.translation import gettext_lazy as _

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

_scriptproject_item = PluginMenuItem(
    link='plugins:netbox_scripts:scriptproject_list',
    link_text=_('Projects'),
    permissions=['netbox_scripts.view_scriptproject'],
    buttons=(
        PluginMenuButton(
            link='plugins:netbox_scripts:scriptproject_add',
            title=_('Add'),
            icon_class='mdi mdi-plus-thick',
            permissions=['netbox_scripts.add_scriptproject'],
        ),
        # The shortest path to a working Project: name it and hand it a script.
        PluginMenuButton(
            link='plugins:netbox_scripts:scriptproject_upload',
            title=_('Upload Script'),
            icon_class='mdi mdi-file-upload-outline',
            # Both, because the upload declares an entrypoint and the view requires the Module
            # half too. A menu button offers no inert state, so a missing half hides it.
            permissions=[
                'netbox_scripts.add_scriptproject',
                'netbox_scripts.add_scriptfile',
            ],
        ),
        PluginMenuButton(
            link='plugins:netbox_scripts:scriptproject_bulk_import',
            title=_('Import'),
            icon_class='mdi mdi-upload',
            permissions=['netbox_scripts.add_scriptproject'],
        ),
    ),
)

# In the Projects group rather than a group of its own: a pass produces Projects.
_migration_item = PluginMenuItem(
    link='plugins:netbox_scripts:migration',
    link_text=_('Migration'),
    permissions=['netbox_scripts.add_scriptproject'],
)

# No add button: rows are derived from an activated revision, never authored.
_netboxscript_item = PluginMenuItem(
    link='plugins:netbox_scripts:netboxscript_list',
    link_text=_('Scripts'),
    permissions=['netbox_scripts.view_netboxscript'],
)

menu = PluginMenu(
    label=_('Custom Scripts'),
    groups=(
        (
            _('Projects'),
            (
                _scriptproject_item,
                _migration_item,
            ),
        ),
        (
            _('Scripts'),
            (_netboxscript_item,),
        ),
    ),
    icon_class='mdi mdi-script-text',
)
