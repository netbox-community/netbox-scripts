from netbox_custom_scripts.models import CustomScriptProjectRevision
from netbox_custom_scripts.tables import CustomScriptProjectRevisionTable, CustomScriptProjectTable
from utilities.testing import TableTestCases


class CustomScriptProjectTableTestCase(TableTestCases.StandardTableTestCase):
    table = CustomScriptProjectTable


class CustomScriptProjectRevisionTableTestCase(TableTestCases.StandardTableTestCase):
    """
    The revision history table, which renders inside the project detail view.

    The source is declared rather than discovered, because discovery looks for a list view
    declaring the table and revisions deliberately have none. They are history rendered on
    their project, not a CRUD surface of their own.
    """

    table = CustomScriptProjectRevisionTable
    queryset_sources = (('CustomScriptProjectView', CustomScriptProjectRevision.objects.all()),)
