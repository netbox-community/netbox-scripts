import shutil
import sys

from django.db import DEFAULT_DB_ALIAS, connections

from core.models import ObjectType
from netbox_scripts.runtime.naming import PRIVATE_ROOT
from netbox_scripts.storage.locks import advisory_key
from users.models import ObjectPermission
from utilities.testing import (  # noqa: F401
    APIViewTestCases,
    ChangeLoggedFilterSetTestMixin,
    ViewTestCases,
)

IN_MEMORY_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


def discard_tree(root):
    """Remove one test tree, restoring the write bits publishing dropped."""
    if root.exists():
        root.chmod(0o755)
        for base, directories, _files in root.walk():
            for name in directories:
                (base / name).chmod(0o755)
    shutil.rmtree(root, ignore_errors=True)


class SecondSession:
    """
    A separate database session, used to observe what this connection holds.

    A session-level advisory lock is only meaningful against another session, so proving
    exclusion needs a real second connection rather than a second cursor.
    """

    def __enter__(self):
        self.connection = connections.create_connection(DEFAULT_DB_ALIAS)
        self.connection.ensure_connection()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.connection.close()
        return False

    def can_lock(self, storage_key):
        """Report whether this other session could take one project's lock right now."""
        namespace, key = advisory_key(storage_key)
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', (namespace, key))
            acquired = cursor.fetchone()[0]
            if acquired:
                cursor.execute('SELECT pg_advisory_unlock(%s, %s)', (namespace, key))
        return acquired


class ObjectPermissionTestMixin:
    """
    Grant one model's actions to the test user as a single Object Permission.

    Core's TestCase.add_permissions covers the unconstrained case, but it takes no constraints
    and keys off an <app>.<action>_<model> name rather than a model class, so a suite testing
    constrained grants needs this instead. A suite that always grants on one model overrides
    grant() to bind it.
    """

    def grant(self, model, *actions, constraints=None):
        permission = ObjectPermission(
            name=f'{model._meta.model_name} {"/".join(actions)}',
            actions=list(actions),
            constraints=constraints,
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(model))
        return permission


def purge_namespace():
    """Drop the runtime namespace from sys.modules, so one test cannot see another's imports."""
    for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
        del sys.modules[name]


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
        ViewTestCases.EditObjectViewTestCase,
        ViewTestCases.ListObjectsViewTestCase,
    ):
        """Composite for a model created and removed through its parent, so read and edit only."""

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

    ``APIViewTestCases.APIViewTestCase`` (from NetBox) covers Get / List /
    Create / Update / Delete / Bulk operations plus GraphQL. Both composites
    below route via the ``plugins-api:`` namespace, the second dropping Create
    and Delete for a viewset that refuses them by method.
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

    class NestedObjectAPIViewTestCase(
        PluginAPIViewTestCase,
        APIViewTestCases.GetObjectViewTestCase,
        APIViewTestCases.ListObjectsViewTestCase,
        APIViewTestCases.UpdateObjectViewTestCase,
        APIViewTestCases.GraphQLTestCase,
    ):
        """Composite for a model whose viewset refuses POST and DELETE by method."""
