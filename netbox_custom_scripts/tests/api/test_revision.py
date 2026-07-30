from django.urls import reverse
from rest_framework import status

from extras.events import serialize_for_event
from netbox_custom_scripts.api.serializers import CustomScriptProjectRevisionSerializer
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage.manifest import compute_digest
from utilities.api import get_serializer_for_model
from utilities.testing import APITestCase


class CustomScriptProjectRevisionSerializerTestCase(APITestCase):
    """
    A revision has no endpoint, but it is change-logged, so it still needs a serializer.

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

    def test_the_serializer_renders_without_a_detail_route(self):
        # url and display_url are reversing identity fields and a revision has no route, so
        # including them would trade a missing serializer for a failed reversal.
        data = CustomScriptProjectRevisionSerializer(self.revision, context={'request': None}).data
        self.assertEqual(data['id'], self.revision.pk)
        self.assertEqual(data['digest'], self.revision.digest)
        self.assertNotIn('url', data)
        self.assertNotIn('display_url', data)

    def test_a_revision_serializes_for_an_event(self):
        self.assertEqual(serialize_for_event(self.revision)['id'], self.revision.pk)

    def test_a_revision_has_no_endpoint_of_its_own(self):
        with self.assertRaises(Exception):
            reverse('plugins-api:netbox_custom_scripts-api:customscriptprojectrevision-list')


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
