from django.test import SimpleTestCase
from django.urls import NoReverseMatch, reverse

from netbox.object_actions import BulkExport
from netbox.tables.columns import ActionsColumn
from netbox_scripts.tables import (
    CustomScriptModuleTable,
    CustomScriptTable,
    ScriptProjectRevisionTable,
    ScriptProjectTable,
)
from netbox_scripts.views import (
    CustomScriptListView,
    CustomScriptModuleListView,
    CustomScriptView,
    ScriptProjectListView,
    ScriptProjectView,
)

LIST_VIEWS = (
    ScriptProjectListView,
    CustomScriptModuleListView,
    CustomScriptListView,
)

# Detail views declare their own action sets too, and an ObjectAction naming an unregistered
# route fails exactly the same way a list button does.
DETAIL_VIEWS = (
    ScriptProjectView,
    CustomScriptView,
)

TABLES = (
    ScriptProjectTable,
    ScriptProjectRevisionTable,
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
                route = f'plugins:netbox_scripts:{model_name}_{action.name}'
                with self.subTest(view=view.__name__, action=action.__name__):
                    try:
                        reverse(route)
                    except NoReverseMatch:
                        self.fail(f'{view.__name__} offers {action.__name__} but {route} is not registered.')

    def test_every_detail_action_resolves_to_a_route(self):
        # Keyed on each action's own url_kwargs, because a detail page mixes actions that take a
        # pk with ones that do not. CloneObject, for instance, points at the model's add route.
        for view in DETAIL_VIEWS:
            model_name = view.queryset.model._meta.model_name
            for action in view.actions:
                if action in ROUTELESS_ACTIONS:
                    continue
                route = f'plugins:netbox_scripts:{model_name}_{action.name}'
                kwargs = dict.fromkeys(action.url_kwargs, 1)
                with self.subTest(view=view.__name__, action=action.__name__):
                    try:
                        reverse(route, kwargs=kwargs)
                    except NoReverseMatch:
                        self.fail(f'{view.__name__} offers {action.__name__} but {route} is not registered.')

    def test_every_row_action_resolves_to_a_route(self):
        # ActionsColumn resolves a URL for every action the viewer holds the permission for,
        # so a default set naming a route the model never registered renders fine for a
        # narrowly permissioned user and raises NoReverseMatch for a superuser. Checking the
        # column statically covers both, which a view test with a scoped user does not.
        #
        # This reaches column.actions only, so extra_buttons is outside it. Those are template
        # strings whose {% url %} tags resolve at render, so they need a view test that renders
        # the table instead: tests/views/test_run.py for the Run button, and the Revisions tab
        # tests for Activate and Deactivate.
        for table in TABLES:
            column = next(
                (col for col in table.base_columns.values() if isinstance(col, ActionsColumn)),
                None,
            )
            if column is None:
                continue
            model_name = table.Meta.model._meta.model_name
            for action in column.actions:
                route = f'plugins:netbox_scripts:{model_name}_{action}'
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


class MenuButtonPermissionsTestCase(SimpleTestCase):
    """
    Every menu button declares every permission its route enforces.

    A menu button has no inert state: the template renders it or it is absent, and it gates on
    user.has_perms, which is all-of. So a button declaring fewer permissions than its view
    enforces sends the operator to a 403 with no form and no explanation.
    """

    @staticmethod
    def buttons():
        """Yield (button, view class) for every button in the plugin's menu."""
        from django.urls import resolve

        from netbox_scripts.navigation import menu

        for group in menu.groups:
            for item in group.items:
                for button in item.buttons:
                    view = resolve(reverse(button.link)).func
                    yield button, getattr(view, 'view_class', None)

    def test_every_menu_button_declares_what_its_view_enforces(self):
        for button, view in self.buttons():
            required = set(getattr(view, 'additional_permissions', ()) or ())
            with self.subTest(button=button.link):
                self.assertTrue(
                    required.issubset(set(button.permissions)),
                    f'{button.link} enforces {sorted(required)} but the button declares {sorted(button.permissions)}.',
                )

    def test_the_menu_offers_at_least_one_button(self):
        # The guard above passes vacuously if the traversal ever stops finding buttons.
        self.assertGreater(len(list(self.buttons())), 0)
