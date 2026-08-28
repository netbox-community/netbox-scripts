from django.urls import reverse

from core.models import ObjectType
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user


class CustomScriptProjectFilesViewTestCase(TestCase):
    """The Files tab lists the current revision's manifest with the live declaration state."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Files Project', key='files-project')
        cls.revision = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            status=RevisionStatusChoices.VALID,
            manifest=[
                {'path': 'deploy.py', 'size': 120, 'sha256': 'b' * 64},
                {'path': 'helpers.py', 'size': 40, 'sha256': 'c' * 64},
            ],
            file_count=2,
            total_size=160,
        )
        CustomScriptModule.objects.create(project=cls.project, source_path='deploy.py', enabled=True)
        CustomScriptModule.objects.create(project=cls.project, source_path='removed.py', enabled=True)
        cls.empty = CustomScriptProject.objects.create(name='Empty Project', key='empty-project')

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def grant(self, model, *actions, constraints=None):
        obj_perm = ObjectPermission(
            name=f'{model._meta.model_name} {"/".join(actions)}',
            actions=list(actions),
            constraints=constraints,
        )
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def url(self, project):
        return reverse('plugins:netbox_custom_scripts:customscriptproject_files', args=[project.pk])

    def test_the_tab_lists_the_manifest(self):
        self.grant(CustomScriptProject, 'view')
        self.grant(CustomScriptProjectRevision, 'view')
        response = self.client.get(self.url(self.project))
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertIn('deploy.py', body)
        self.assertIn('helpers.py', body)
        # The short digest form, matching the revision history table.
        self.assertIn('b' * 12, body)
        self.assertNotIn('b' * 64, body)
        self.assertIn('120', body)

    def test_a_declared_path_missing_from_the_source_is_marked(self):
        self.grant(CustomScriptProject, 'view')
        self.grant(CustomScriptProjectRevision, 'view')
        body = self.client.get(self.url(self.project)).content.decode()
        self.assertIn('removed.py (missing from the source)', body)

    def test_the_entrypoint_column_reads_the_live_declaration(self):
        self.grant(CustomScriptProject, 'view')
        self.grant(CustomScriptProjectRevision, 'view')
        table = self.client.get(self.url(self.project)).context['table']
        state = {row.record['path']: row.record['entrypoint'] for row in table.rows}
        self.assertTrue(state['deploy.py'])
        self.assertFalse(state['helpers.py'])
        self.assertTrue(state['removed.py'])

    def test_the_tab_is_empty_without_permission_on_its_revision(self):
        self.grant(CustomScriptProject, 'view')

        response = self.client.get(self.url(self.project))

        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertNotIn('deploy.py', body)
        self.assertNotIn('a' * 64, body)

    def test_a_project_without_content_shows_the_empty_state(self):
        self.grant(CustomScriptProject, 'view')
        response = self.client.get(self.url(self.empty))
        self.assertHttpStatus(response, 200)
        self.assertIn('This project has no stored revision yet.', response.content.decode())

    def test_the_view_permission_is_required(self):
        self.assertHttpStatus(self.client.get(self.url(self.project)), 403)

    def test_the_tab_is_linked_from_the_detail_page(self):
        self.grant(CustomScriptProject, 'view')
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(self.url(self.project), body)
