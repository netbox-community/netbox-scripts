import importlib.util
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from netbox_scripts import config

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / 'scripts' / 'check_netbox_internals.py'
DOCS = REPOSITORY / 'docs' / 'development' / 'netbox-internals.md'


def load_canary():
    spec = importlib.util.spec_from_file_location('check_netbox_internals', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def table_symbols():
    # The Symbol column of the page's table, backticks stripped, in table order.
    section = DOCS.read_text().split('## Dependency ledger', 1)[1].split('\n## ', 1)[0]
    return [line.split('|')[1].strip().replace('`', '') for line in section.splitlines() if line.startswith('| `')]


@unittest.skipUnless(
    SCRIPT.is_file() and DOCS.is_file(), 'the canary ships with the repository, not inside the package'
)
class InternalsCanaryTestCase(SimpleTestCase):
    """The script CI runs against the feature ref, run here against the host the suite runs on."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.canary = load_canary()

    def test_every_listed_crossing_resolves_on_this_host(self):
        self.assertEqual(self.canary.check(), [])

    def test_the_lock_namespaces_are_clear_of_every_core_key(self):
        self.assertEqual(self.canary.check_lock_keys(), [])

    def test_the_plugin_is_loaded_on_this_host(self):
        self.assertEqual(self.canary.check_plugin_loaded(), [])

    def test_every_table_row_has_a_probe_group_in_the_same_order(self):
        self.assertEqual([label for label, *_ in self.canary.ROWS], table_symbols())

    def test_every_row_carries_at_least_one_probe(self):
        self.assertEqual([label for label, *probes in self.canary.ROWS if not probes], [])

    def test_a_plugin_skipped_at_settings_load_is_reported(self):
        with mock.patch('django.apps.apps.is_installed', return_value=False):
            failures = self.canary.check_plugin_loaded()
        self.assertEqual(len(failures), 1)
        self.assertIn('skipped at settings load', failures[0])
        self.assertIn(config.max_version, failures[0])

    def test_a_missing_attribute_is_reported_under_its_row(self):
        rows = (('the row', 'extras.models:EventRule.enabled', 'extras.models:EventRule.no_such_field'),)
        failures = self.canary.check(rows)
        self.assertEqual(len(failures), 1)
        self.assertTrue(failures[0].startswith('the row: extras.models:EventRule.no_such_field: AttributeError'))

    def test_a_missing_module_and_a_missing_registry_key_are_reported(self):
        self.assertIn('ModuleNotFoundError', self.canary.resolve('extras.no_such_module:Thing'))
        self.assertIn('KeyError', self.canary.resolve("netbox.registry:registry['no_such_key']"))

    def test_a_malformed_probe_is_refused_rather_than_resolved_in_part(self):
        # Without the shape check, findall() would read Job..fetch as Job.fetch and pass it.
        malformed = (
            'extras.models',
            'extras.models:',
            'rq.job:Job..fetch',
            'netbox.registry:registry[request_processors]',
        )
        for probe in malformed:
            with self.subTest(probe=probe), self.assertRaises(ValueError):
                self.canary.resolve(probe)

    def test_a_core_key_on_a_claimed_namespace_is_reported(self):
        with mock.patch.dict('netbox.constants.ADVISORY_LOCK_KEYS', {'new-core-lock': 770101}):
            failures = self.canary.check_lock_keys()
        self.assertEqual(len(failures), 1)
        self.assertIn("'new-core-lock'", failures[0])
        self.assertIn('MIGRATION_LOCK_NAMESPACE', failures[0])
