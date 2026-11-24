"""User preferences the plugin adds to NetBox's Preferences page."""

from django.utils.translation import gettext_lazy as _

from users.preferences import UserPreference

from .choices import ScriptListViewChoices

SCRIPT_LIST_VIEW = 'plugins.netbox_scripts.script_list_view'

preferences = {
    'script_list_view': UserPreference(
        label=_('Scripts list view'),
        choices=ScriptListViewChoices.CHOICES,
        default=ScriptListViewChoices.TABLE,
        description=_('Show the Scripts list as a table or grouped by Project'),
    ),
}
