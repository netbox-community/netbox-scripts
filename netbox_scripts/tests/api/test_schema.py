import json

from django.test import TestCase, override_settings
from drf_spectacular.drainage import GENERATOR_STATS

RUN = '/api/plugins/netbox-scripts/scripts/{id}/run/'
UPLOAD = '/api/plugins/netbox-scripts/projects/{id}/upload/'
SCRIPT_FILES = '/api/plugins/netbox-scripts/projects/{id}/script-files/'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.dummy.DummyCache'}})
class OpenAPISchemaTestCase(TestCase):
    """Each custom action publishes the contract it implements, not the viewset's model serializer."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Generation walks the whole NetBox API.
        with GENERATOR_STATS.silence():
            response = cls.client_class().get('/api/schema/', {'format': 'json'})
        cls().assertEqual(response.status_code, 200, 'the schema endpoint did not answer')
        cls.schema = json.loads(response.content)

    def request_ref(self, path, method, content='application/json'):
        """Return the component reference a request body names, or None if the type is not offered."""
        body = self.schema['paths'][path][method].get('requestBody', {})
        entry = body.get('content', {}).get(content)
        return entry['schema'].get('$ref') if entry else None

    def response_ref(self, path, method, code):
        """Return the component reference a response names, or None if the code is not declared."""
        declared = self.schema['paths'][path][method]['responses'].get(code)
        if declared is None:
            return None
        return declared['content']['application/json']['schema'].get('$ref')

    def test_run_takes_the_run_input_and_returns_a_job(self):
        self.assertEqual(self.request_ref(RUN, 'post'), '#/components/schemas/NetBoxScriptRunInputRequest')
        self.assertEqual(self.response_ref(RUN, 'post', '201'), '#/components/schemas/Job')
        # The inferred contract reported the Script at 200.
        self.assertIsNone(self.response_ref(RUN, 'post', '200'))

    def test_upload_takes_a_file_and_returns_a_revision(self):
        self.assertEqual(
            self.request_ref(UPLOAD, 'post', content='multipart/form-data'),
            '#/components/schemas/ScriptProjectUploadRequest',
        )
        self.assertEqual(
            self.response_ref(UPLOAD, 'post', '201'),
            '#/components/schemas/ScriptProjectRevision',
        )
        self.assertIsNone(self.response_ref(UPLOAD, 'post', '200'))

    def test_script_files_reports_candidates_and_takes_paths(self):
        self.assertEqual(
            self.response_ref(SCRIPT_FILES, 'get', '200'),
            '#/components/schemas/ScriptProjectScriptFiles',
        )
        self.assertEqual(
            self.request_ref(SCRIPT_FILES, 'put'),
            '#/components/schemas/ScriptProjectScriptFileSelectionRequest',
        )
        self.assertEqual(
            self.response_ref(SCRIPT_FILES, 'put', '200'),
            '#/components/schemas/ScriptProjectScriptFiles',
        )

    def test_script_file_state_declares_the_fields_the_action_returns(self):
        component = self.schema['components']['schemas']['ScriptProjectScriptFiles']
        self.assertEqual(sorted(component['properties']), ['candidates', 'mode'])
        candidate = self.schema['components']['schemas']['ScriptProjectScriptFileCandidate']
        self.assertEqual(
            sorted(candidate['properties']),
            ['available', 'discovery_status', 'path', 'selected'],
        )
