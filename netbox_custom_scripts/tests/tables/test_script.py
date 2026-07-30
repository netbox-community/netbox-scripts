from netbox_custom_scripts.tables import CustomScriptTable
from utilities.testing import TableTestCases


class CustomScriptTableTestCase(TableTestCases.StandardTableTestCase):
    table = CustomScriptTable
