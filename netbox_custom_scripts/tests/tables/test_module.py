from netbox_custom_scripts.tables import CustomScriptModuleTable
from utilities.testing import TableTestCases


class CustomScriptModuleTableTestCase(TableTestCases.StandardTableTestCase):
    table = CustomScriptModuleTable
