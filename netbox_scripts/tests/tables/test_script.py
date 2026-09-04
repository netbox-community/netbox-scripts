from django.test import TestCase

from netbox_scripts.tables import CustomScriptLogTable, CustomScriptTable
from utilities.testing import TableTestCases


class CustomScriptTableTestCase(TableTestCases.StandardTableTestCase):
    table = CustomScriptTable


class CustomScriptLogTableTestCase(TestCase):
    """The run log links a logged object, and refuses a scheme a browser must not follow."""

    def row(self, url):
        return [{'index': 1, 'time': None, 'status': 'info', 'object': 'thing', 'url': url, 'message': 'ran'}]

    def rendered(self, url):
        table = CustomScriptLogTable(self.row(url))
        return ''.join(str(cell) for cell in table.rows[0].get_cell('object'))

    def test_a_relative_url_is_linked(self):
        self.assertIn('href="/dcim/sites/1/"', self.rendered('/dcim/sites/1/'))

    def test_a_non_string_url_does_not_crash_the_column(self):
        for url in (5, True, ['/x'], {'a': 1}):
            with self.subTest(url=url):
                self.assertNotIn('href=', self.rendered(url))

    def test_a_javascript_url_is_rendered_as_plain_text(self):
        # format_html escapes for HTML context but does not constrain the scheme.
        rendered = self.rendered('javascript:alert(1)')

        self.assertNotIn('href=', rendered)
        self.assertIn('thing', rendered)
