from utilities.testing import APIViewTestCases, ViewTestCases

try:
    from utilities.testing import ChangeLoggedFilterSetTestMixin
except ImportError:
    # NetBox 4.6 ships the pre-rename name. Drop this fallback at the 4.7 floor switch.
    from utilities.testing import ChangeLoggedFilterSetTests as ChangeLoggedFilterSetTestMixin  # noqa: F401


class PluginViewTestCase:
    """Prepend the ``plugins:`` namespace when reversing UI view names."""

    def _get_base_url(self):
        viewname = super()._get_base_url()
        return f'plugins:{viewname}'


class PluginAPIViewTestCase:
    """Point the API test case at the plugin's ``plugins-api:`` namespace."""

    def _get_view_namespace(self):
        return f'plugins-api:{self.model._meta.app_label}-api'


class PluginTestCases:
    """Plugin-aware variants of NetBox's view test cases.

    NetBox plugin URLs live under the ``plugins:`` (UI) and
    ``plugins-api:`` (REST API) namespaces. ``PluginViewTestCase`` /
    ``PluginAPIViewTestCase`` route ``reverse()`` through the right
    namespace. Compose them with ``ViewTestCases.PrimaryObjectViewTestCase``
    so all standard primary-object views (Get / Edit / Delete / List /
    BulkEdit / BulkDelete / BulkImport) test cleanly.
    """

    class PrimaryObjectViewTestCase(
        PluginViewTestCase,
        ViewTestCases.PrimaryObjectViewTestCase,
    ):
        """Composite for first-class plugin models."""

        maxDiff = None

    class NestedObjectViewTestCase(
        PluginViewTestCase,
        ViewTestCases.GetObjectViewTestCase,
        ViewTestCases.GetObjectChangelogViewTestCase,
        ViewTestCases.CreateObjectViewTestCase,
        ViewTestCases.EditObjectViewTestCase,
        ViewTestCases.DeleteObjectViewTestCase,
        ViewTestCases.ListObjectsViewTestCase,
        ViewTestCases.BulkDeleteObjectsViewTestCase,
    ):
        """Composite for a model managed through its parent, so no bulk import or bulk edit."""

        maxDiff = None

    class DerivedObjectViewTestCase(
        PluginViewTestCase,
        ViewTestCases.GetObjectViewTestCase,
        ViewTestCases.GetObjectChangelogViewTestCase,
        ViewTestCases.EditObjectViewTestCase,
        ViewTestCases.ListObjectsViewTestCase,
        ViewTestCases.BulkEditObjectsViewTestCase,
    ):
        """Composite for a model whose rows are derived, so no create, delete, or import."""

        maxDiff = None


class PluginAPIViewTestCases:
    """Plugin-aware variants of the standard API view test cases.

    ``APIViewTestCases.APIViewTestCase`` (NetBox-core) covers Get / List /
    Create / Update / Delete / Bulk operations plus GraphQL. The composite
    below mirrors it but routes via the ``plugins-api:`` namespace.
    """

    class APIViewTestCase(
        PluginAPIViewTestCase,
        APIViewTestCases.GetObjectViewTestCase,
        APIViewTestCases.ListObjectsViewTestCase,
        APIViewTestCases.CreateObjectViewTestCase,
        APIViewTestCases.UpdateObjectViewTestCase,
        APIViewTestCases.DeleteObjectViewTestCase,
        APIViewTestCases.GraphQLTestCase,
    ):
        """Composite for first-class plugin models."""
