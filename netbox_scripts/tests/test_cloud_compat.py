import ast
import importlib.util
import pathlib

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils.module_loading import import_string

from netbox_scripts.tests.plugin_testing import pickle_queued_objects

PIPELINE_ENTRY = 'netbox_scripts.tests.plugin_testing.pickle_queued_objects'


class ContractCheckerTestCase(TestCase):
    """
    Cover the reach of the AST gate that polices the platform contract.

    The gate is the other half of the contract the backend contract cases prove
    behaviorally, and a name missing from its tables fails silently: nothing breaks, the
    gate simply stops seeing a whole class of breach. Only a test notices that.
    """

    @staticmethod
    def load_checker():
        repository = pathlib.Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location('_cloud_compat', repository / 'scripts' / 'check_cloud_compat.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def findings_for(self, source):
        checker = self.load_checker()
        tree = ast.parse(source)
        visitor = checker.ContractVisitor(
            pathlib.Path('probe.py'), source.splitlines(), checker.collect_docstrings(tree)
        )
        visitor.visit(tree)
        return sorted(finding.subject for finding in visitor.findings)

    def test_every_pathlib_write_is_seen(self):
        # Each os.* equivalent is already forbidden, so permitting the pathlib spelling would
        # let a module do all of its local writing unwaived, which is how the cache tier passed.
        source = (
            'from pathlib import Path\n'
            '\n'
            '\n'
            'def leak(target: Path):\n'
            '    target.mkdir()\n'
            '    target.chmod(0o777)\n'
            '    target.unlink()\n'
            '    target.rmdir()\n'
            '    target.rename(target)\n'
            '    target.touch()\n'
            '    target.write_text("x")\n'
            '    target.write_bytes(b"x")\n'
        )
        self.assertEqual(
            self.findings_for(source),
            [
                '.chmod()',
                '.mkdir()',
                '.rename()',
                '.rmdir()',
                '.touch()',
                '.unlink()',
                '.write_bytes()',
                '.write_text()',
            ],
        )

    def test_a_string_method_sharing_a_name_is_not_a_finding(self):
        # str.replace is why .replace stays out of the method table.
        self.assertEqual(self.findings_for('def clean(text):\n    return text.replace("a", "b")\n'), [])

    def test_the_waiver_marker_still_exempts_a_sanctioned_write(self):
        source = 'from pathlib import Path\n\n\ndef ok(target: Path):\n    target.mkdir()  # cloud-compat: ok, reason\n'
        self.assertEqual(self.findings_for(source), [])

    def test_a_request_stored_on_another_object_is_a_finding(self):
        self.assertEqual(self.findings_for('obj._request = request\n'), ['obj._request = request'])

    def test_the_view_request_stored_on_a_form_instance_is_a_finding(self):
        source = 'self.instance._request = self.request\n'
        self.assertEqual(self.findings_for(source), ['self.instance._request = self.request'])

    def test_a_request_stored_through_setattr_is_a_finding(self):
        source = "setattr(obj, '_request', request)\n"
        self.assertEqual(self.findings_for(source), ["setattr(obj, '_request', request)"])

    def test_the_current_request_stored_on_an_object_is_a_finding(self):
        source = 'from netbox.context import current_request\nobj._request = current_request.get()\n'
        self.assertEqual(self.findings_for(source), ['obj._request = current_request.get()'])

    def test_a_request_kept_on_self_is_not_a_finding(self):
        self.assertEqual(self.findings_for('self.request = request\n'), [])

    def test_the_user_read_from_a_request_is_not_a_finding(self):
        self.assertEqual(self.findings_for('obj._user = request.user\n'), [])


class EventPipelineTestCase(SimpleTestCase):
    """The test configuration pickles every queued object, as a platform consumer may."""

    def test_the_pickling_consumer_is_in_the_pipeline(self):
        self.assertIn(PIPELINE_ENTRY, settings.EVENTS_PIPELINE)
        # NetBox only logs a pipeline entry it cannot import, so a stale path would disable the check silently.
        self.assertIs(import_string(PIPELINE_ENTRY), pickle_queued_objects)
