from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

from netbox.object_actions import BulkExport
from netbox.tables.columns import ActionsColumn
from netbox_custom_scripts.tables import (
    CustomScriptModuleTable,
    CustomScriptProjectRevisionTable,
    CustomScriptProjectTable,
    CustomScriptTable,
)
from netbox_custom_scripts.views import (
    CustomScriptListView,
    CustomScriptModuleListView,
    CustomScriptProjectListView,
)

LIST_VIEWS = (
    CustomScriptProjectListView,
    CustomScriptModuleListView,
    CustomScriptListView,
)

TABLES = (
    CustomScriptProjectTable,
    CustomScriptProjectRevisionTable,
    CustomScriptModuleTable,
    CustomScriptTable,
)

# BulkExport posts query parameters back to the list route, so it is the one action that
# needs no route of its own.
ROUTELESS_ACTIONS = (BulkExport,)


class ListViewActionsTestCase(SimpleTestCase):
    """
    Every button a list view offers must lead somewhere.

    ActionsMixin filters the default action set by permission alone, never by whether the
    route exists, so inheriting the default silently renders buttons for views the plugin
    never registered.
    """

    def test_every_list_action_resolves_to_a_route(self):
        for view in LIST_VIEWS:
            model_name = view.queryset.model._meta.model_name
            for action in view.actions:
                if action in ROUTELESS_ACTIONS:
                    continue
                route = f'plugins:netbox_custom_scripts:{model_name}_{action.name}'
                with self.subTest(view=view.__name__, action=action.__name__):
                    try:
                        reverse(route)
                    except NoReverseMatch:
                        self.fail(f'{view.__name__} offers {action.__name__} but {route} is not registered.')

    def test_every_row_action_resolves_to_a_route(self):
        # ActionsColumn resolves a URL for every action the viewer holds the permission for,
        # so a default set naming a route the model never registered renders fine for a
        # narrowly permissioned user and raises NoReverseMatch for a superuser. Checking the
        # column statically covers both, which a view test with a scoped user does not.
        for table in TABLES:
            column = next(
                (col for col in table.base_columns.values() if isinstance(col, ActionsColumn)),
                None,
            )
            if column is None:
                continue
            model_name = table.Meta.model._meta.model_name
            for action in column.actions:
                route = f'plugins:netbox_custom_scripts:{model_name}_{action}'
                with self.subTest(table=table.__name__, action=action):
                    try:
                        reverse(route, kwargs={'pk': 1})
                    except NoReverseMatch:
                        self.fail(f'{table.__name__} offers a row {action} action but {route} is not registered.')

    def test_no_list_view_inherits_the_default_action_set(self):
        # The default is the failure mode above, so declaring actions explicitly is the fix
        # and this pins that none of them drifts back.
        from netbox.views.generic import ObjectListView

        for view in LIST_VIEWS:
            with self.subTest(view=view.__name__):
                self.assertIsNot(view.actions, ObjectListView.actions)
