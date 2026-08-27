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
    """The update-only Custom Script endpoint and the identity fields NetBox reverses."""

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
        # Nested, so the revision's own reversing field resolves through this caller too.
        self.assertEqual(data['last_seen_revision']['id'], self.revision.pk)
        self.assertEqual(
            data['last_seen_revision']['url'],
            f'/api/plugins/custom-scripts/project-revisions/{self.revision.pk}/',
        )

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

    def test_authoring_and_deleting_are_refused(self):
        # Rows are derived from an activated revision, so no verb may author or destroy one.
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
            (self.client.delete, self._get_detail_url(self.script)),
        ):
            with self.subTest(method=method.__name__):
                response = method(url, payload, format='json', **self.header)
                self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

        self.assertEqual(CustomScript.objects.count(), 2)

    def test_enabled_is_patchable(self):
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )
        response = self.client.patch(
            self._get_detail_url(self.script), {'enabled': False}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertFalse(self.script.enabled)

    def test_the_execution_overrides_are_patchable(self):
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )
        response = self.client.patch(
            self._get_detail_url(self.script),
            {'commit_default_override': False, 'job_timeout_override': 45, 'notifications_default_override': 'never'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertIs(self.script.commit_default_override, False)
        self.assertEqual(self.script.job_timeout_override, 45)
        self.assertEqual(self.script.notifications_default_override, 'never')

    def test_a_nested_revision_carries_the_choice_pair_too(self):
        # status is in the revision's brief_fields, so the nested payload changed shape with the
        # serializer. Pinned here because a nested representation is the easiest one to miss.
        self.add_permissions('netbox_custom_scripts.view_customscript')
        response = self.client.get(self._get_detail_url(self.script), **self.header)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            response.data['last_seen_revision']['status']['value'],
            self.script.last_seen_revision.status,
        )

    def test_a_null_clears_the_notification_override_like_the_other_two(self):
        # Without a ChoiceField the CharField refuses null, so a client clearing all three
        # overrides would need two different sentinels.
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )
        CustomScript.objects.filter(pk=self.script.pk).update(notifications_default_override='never')
        response = self.client.patch(
            self._get_detail_url(self.script),
            {'notifications_default_override': None},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertEqual(self.script.notifications_default_override, '')

    def test_an_unset_choice_override_reads_as_null(self):
        self.add_permissions('netbox_custom_scripts.view_customscript')
        response = self.client.get(self._get_detail_url(self.script), **self.header)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['notifications_default_override'])

    def test_an_override_is_clearable_over_rest(self):
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )
        CustomScript.objects.filter(pk=self.script.pk).update(job_timeout_override=45)
        response = self.client.patch(
            self._get_detail_url(self.script), {'job_timeout_override': None}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertIsNone(self.script.job_timeout_override)

    def test_a_derived_field_supplied_to_a_patch_is_ignored(self):
        # A read-only field is silently dropped by DRF. Asserting it stops a future serializer
        # edit from quietly opening a write path to a synchronization-owned column.
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )
        response = self.client.patch(
            self._get_detail_url(self.script),
            {'display_name': 'Renamed', 'is_retired': True, 'module_path': 'moved'},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertEqual(self.script.display_name, 'Deploy Devices')
        self.assertEqual(self.script.module_path, 'deploy')
        self.assertFalse(self.script.is_retired)

    def test_a_patch_cannot_move_a_script_to_another_project(self):
        # project is a declared field, and DRF ignores Meta.read_only_fields for those, so a
        # plain change token could reparent a derived row and take its Job history with it.
        other = CustomScriptProject.objects.create(name='Other Project', key='other-project')
        self.add_permissions(
            'netbox_custom_scripts.view_customscript',
            'netbox_custom_scripts.change_customscript',
        )

        response = self.client.patch(
            self._get_detail_url(self.script), {'project': other.pk}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.script.refresh_from_db()
        self.assertEqual(self.script.project_id, self.project.pk)

    def test_patching_requires_the_change_permission(self):
        self.add_permissions('netbox_custom_scripts.view_customscript')
        response = self.client.patch(
            self._get_detail_url(self.script), {'enabled': False}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
