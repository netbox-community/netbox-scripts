from netbox_custom_scripts.tables import CustomScriptProjectTable
from utilities.testing import TableTestCases


class CustomScriptProjectTableTestCase(TableTestCases.StandardTableTestCase):
    table = CustomScriptProjectTable
