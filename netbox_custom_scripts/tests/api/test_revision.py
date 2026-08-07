from django.urls import reverse
from rest_framework import status

from extras.events import serialize_for_event
from netbox_custom_scripts.api.serializers import CustomScriptProjectRevisionSerializer
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.tests.plugin_testing import PluginAPIViewTestCase
from utilities.api import get_serializer_for_model
from utilities.testing import APITestCase


class CustomScriptProjectRevisionSerializerTestCase(APITestCase):
    """
    The revision serializer, and the caller that resolves it by model name.

    Delete events serialize eagerly and resolve the serializer by model name, and deleting a
    project cascades its revisions, so this path runs on any request-bound project delete even
    when nothing ever activated a revision in a request.
    """

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='API Revision Project', key='api-revision-project')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=compute_digest([]),
            status=RevisionStatusChoices.VALID,
        )

    def test_the_serializer_resolves_by_model_name(self):
        # The path event serialization takes. It composes the class name from the app label and
        # the model, so the class has to be importable from the package root by exactly that name.
        self.assertIs(get_serializer_for_model(CustomScriptProjectRevision), CustomScriptProjectRevisionSerializer)

    def test_the_serializer_renders_without_a_request(self):
        # Event serialization passes request=None, which yields relative identity URLs rather
        # than raising, so carrying url and display_url does not break that caller.
        data = CustomScriptProjectRevisionSerializer(self.revision, context={'request': None}).data
        self.assertEqual(data['id'], self.revision.pk)
        self.assertEqual(data['digest'], self.revision.digest)
        self.assertIn('url', data)
        self.assertIn('display_url', data)

    def test_a_revision_serializes_for_an_event(self):
        self.assertEqual(serialize_for_event(self.revision)['id'], self.revision.pk)


class CustomScriptProjectRevisionAPIViewTestCase(PluginAPIViewTestCase, APITestCase):
    """The read-only revision endpoint, and the four writes it refuses."""

    model = CustomScriptProjectRevision

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Revision Endpoint', key='revision-endpoint')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=compute_digest([]),
            status=RevisionStatusChoices.VALID,
            manifest=[{'path': 'deploy.py', 'size': 3, 'sha256': 'a' * 64}],
            file_count=1,
            total_size=3,
        )

    def _list_url(self):
        return reverse('plugins-api:netbox_custom_scripts-api:customscriptprojectrevision-list')

    def _detail_url(self):
        return reverse(
            'plugins-api:netbox_custom_scripts-api:customscriptprojectrevision-detail', args=[self.revision.pk]
        )

    def test_the_list_route_returns_revisions(self):
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._list_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)

    def test_the_detail_route_returns_one_revision(self):
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._detail_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(response.data['digest'], self.revision.digest)
        # A plain string, matching how every other serializer here renders a choice field.
        self.assertEqual(response.data['status'], RevisionStatusChoices.VALID)

    def test_the_detail_url_reverses(self):
        """The reason url and display_url came back."""
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._detail_url(), **self.header)
        self.assertTrue(
            response.data['url'].endswith(f'/api/plugins/custom-scripts/project-revisions/{self.revision.pk}/')
        )
        self.assertTrue(response.data['display_url'].endswith(f'/plugins/custom-scripts/revisions/{self.revision.pk}/'))

    def test_the_manifest_is_absent(self):
        """Large and internal, and it belongs on the diagnostics surface instead."""
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._detail_url(), **self.header)
        self.assertNotIn('manifest', response.data)
        self.assertNotIn('entrypoint_snapshot', response.data)

    def test_the_lease_fields_are_absent(self):
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._detail_url(), **self.header)
        self.assertNotIn('validation_job', response.data)
        self.assertNotIn('validation_started', response.data)

    def test_the_storage_key_is_absent(self):
        """It is the content-addressing identity of an operator's stored bytes."""
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(self._detail_url(), **self.header)
        self.assertNotIn('storage_key', response.data)
        self.assertNotIn('storage_key', response.data['project'])

    def test_post_is_refused(self):
        self.add_permissions('netbox_custom_scripts.add_customscriptprojectrevision')
        response = self.client.post(self._list_url(), {'project': self.project.pk}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_put_is_refused(self):
        self.add_permissions('netbox_custom_scripts.change_customscriptprojectrevision')
        response = self.client.put(self._detail_url(), {'status': 'active'}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patch_is_refused(self):
        self.add_permissions('netbox_custom_scripts.change_customscriptprojectrevision')
        response = self.client.patch(self._detail_url(), {'status': 'active'}, format='json', **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_delete_is_refused(self):
        self.add_permissions('netbox_custom_scripts.delete_customscriptprojectrevision')
        response = self.client.delete(self._detail_url(), **self.header)
        self.assertHttpStatus(response, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_the_project_filter_narrows_the_list(self):
        other = CustomScriptProject.objects.create(name='Other Revisions', key='other-revisions')
        CustomScriptProjectRevision.objects.create(project=other, digest='b' * 64)
        self.add_permissions('netbox_custom_scripts.view_customscriptprojectrevision')
        response = self.client.get(f'{self._list_url()}?project_id={self.project.pk}', **self.header)
        self.assertEqual(response.data['count'], 1)


class ProjectDeleteEventSerializationTestCase(APITestCase):
    """Deleting an activated project over REST, the path that raised SerializerNotFound."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='API Delete Project', key='api-delete-project')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=compute_digest([]),
            status=RevisionStatusChoices.ACTIVE,
        )
        cls.project.active_revision = cls.revision
        cls.project.save()
        CustomScript.objects.create(
            project=cls.project,
            module_path='deploy',
            class_name='Deploy',
            display_name='Deploy',
            last_seen_revision=cls.revision,
        )

    def test_deleting_an_active_project_over_rest_succeeds(self):
        self.add_permissions('netbox_custom_scripts.delete_customscriptproject')
        url = reverse('plugins-api:netbox_custom_scripts-api:customscriptproject-detail', args=[self.project.pk])
        response = self.client.delete(url, **self.header)
        self.assertHttpStatus(response, status.HTTP_204_NO_CONTENT)
        self.assertFalse(CustomScriptProject.objects.filter(pk=self.project.pk).exists())
        self.assertFalse(CustomScriptProjectRevision.objects.filter(pk=self.revision.pk).exists())
        self.assertFalse(CustomScript.objects.exists())
