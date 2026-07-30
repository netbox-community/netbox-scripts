from django.urls import reverse
from rest_framework import status

from extras.events import serialize_for_event
from netbox_custom_scripts.api.serializers import CustomScriptSerializer
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.tests.plugin_testing import PluginAPIViewTestCase
from utilities.api import get_serializer_for_model
from utilities.testing import APITestCase

DIGEST = 'e' * 64


class CustomScriptAPIViewTestCase(PluginAPIViewTestCase, APITestCase):
    """The read-only Custom Script endpoint and the identity fields NetBox reverses."""

    model = CustomScript

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='API Script Project', key='api-script-project')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        cls.script = CustomScript.objects.create(
            project=cls.project,
            module_path='deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            last_seen_revision=cls.revision,
            metadata={'commit_default': True, 'job_timeout': 300},
        )
        CustomScript.objects.create(
            project=cls.project,
            module_path='helpers',
            class_name='Retired',
            display_name='Retired',
            is_retired=True,
        )

    def test_the_serializer_is_resolvable_from_the_package_path(self):
        # serialize_for_event() composes this path from the app label and model name, so a
        # package-layout refactor must not move the class out of it.
        self.assertIs(get_serializer_for_model(CustomScript), CustomScriptSerializer)

    def test_the_identity_fields_resolve_without_a_request(self):
        # url is a reversing field, so it fails outright if no API route is registered.
        data = CustomScriptSerializer(self.script, context={'request': None}).data
        self.assertEqual(data['url'], f'/api/plugins/custom-scripts/scripts/{self.script.pk}/')
        self.assertEqual(data['display'], 'Deploy Devices')
        self.assertEqual(data['project']['id'], self.project.pk)
        self.assertEqual(data['last_seen_revision'], self.revision.pk)

    def test_serialize_for_event_returns_data(self):
        data = serialize_for_event(self.script)
        self.assertEqual(data['id'], self.script.pk)
        self.assertEqual(data['class_name'], 'DeployDevices')

    def test_deleting_the_project_serializes_the_cascading_script_events(self):
        # Delete events serialize eagerly, unlike create and update, so this path needs the
        # serializer even though activation never runs in this request.
        project = CustomScriptProject.objects.create(name='Cascade Project', key='cascade-project')
        CustomScript.objects.create(
            project=project,
            module_path='deploy',
            class_name='Cascaded',
            display_name='Cascaded',
        )
        self.add_permissions(
            'netbox_custom_scripts.delete_customscriptproject',
            'netbox_custom_scripts.delete_customscript',
            'netbox_custom_scripts.view_customscriptproject',
        )
        self.client.force_login(self.user)

        url = reverse('plugins:netbox_custom_scripts:customscriptproject_delete', kwargs={'pk': project.pk})
        response = self.client.post(url, {'confirm': True}, follow=False)

        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_302_FOUND))
        self.assertFalse(CustomScript.objects.filter(project_id=project.pk).exists())
        self.assertFalse(CustomScriptProject.objects.filter(pk=project.pk).exists())

    def test_list_and_detail_return_200_with_permission(self):
        self.add_permissions('netbox_custom_scripts.view_customscript')

        response = self.client.get(self._get_list_url(), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 2)

        response = self.client.get(self._get_detail_url(self.script), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['class_name'], 'DeployDevices')

    def test_the_detail_endpoint_requires_permission(self):
        response = self.client.get(self._get_detail_url(self.script), **self.header)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_writes_are_refused(self):
        # Rows are derived from an activated revision, so no verb may author them.
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.add_customscript',
            'netbox_custom_scripts.change_customscript',
            'netbox_custom_scripts.delete_customscript',
        )
        payload = {
            'project': self.project.pk,
            'module_path': 'injected',
            'class_name': 'Injected',
        }
        for method, url in (
            (self.client.post, self._get_list_url()),
            (self.client.put, self._get_detail_url(self.script)),
            (self.client.patch, self._get_detail_url(self.script)),
            (self.client.delete, self._get_detail_url(self.script)),
        ):
            with self.subTest(method=method.__name__):
                response = method(url, payload, format='json', **self.header)
                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        self.assertEqual(CustomScript.objects.count(), 2)
