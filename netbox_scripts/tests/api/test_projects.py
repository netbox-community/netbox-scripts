import uuid
from unittest import mock

from rest_framework import status

from core.models import DataSource, ObjectType
from netbox_scripts.api.serializers import ScriptProjectSerializer
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_scripts.models import ScriptProject
from netbox_scripts.tests.plugin_testing import PluginAPIViewTestCases
from users.models import ObjectPermission


class ScriptProjectAPIViewTestCase(PluginAPIViewTestCases.APIViewTestCase):
    model = ScriptProject
    # The root fields carry the plugin prefix, which the verbose name the mixin derives from does not.
    graphql_base_name = 'netbox_script_project'
    brief_fields = ['description', 'display', 'id', 'key', 'name', 'url']
    # Explicit update_data: the default falls back to create_data[0], whose 'key'
    # differs from the updated instance's, and key is immutable after creation.
    update_data = {
        'name': 'ScriptProject 1 Updated',
        'description': 'Updated description',
        'enabled': False,
    }
    bulk_update_data = {
        'description': 'Bulk-updated description',
        'enabled': False,
    }
    # Refused by the serializer's ChoiceField.
    bulk_update_invalid_data = {'activation_policy': 'not-a-policy'}

    @classmethod
    def setUpTestData(cls):
        ScriptProject.objects.create(name='ScriptProject 1', key='project-1', description='First project')
        ScriptProject.objects.create(name='ScriptProject 2', key='project-2', description='Second project')
        ScriptProject.objects.create(name='ScriptProject 3', key='project-3', enabled=False)

        cls.create_data = [
            {'name': 'ScriptProject 4', 'key': 'project-4', 'description': 'Fourth project'},
            {'name': 'ScriptProject 5', 'key': 'project-5', 'description': 'Fifth project'},
            {'name': 'ScriptProject 6', 'key': 'project-6', 'description': '', 'enabled': False},
        ]

    def synchronized(self, **kwargs):
        """A Data Source-backed project, which is what makes all three gated fields movable."""
        source = DataSource.objects.create(
            name=f'Gate Source {uuid.uuid4().hex[:8]}', type='local', source_url='file:///tmp/gate/'
        )
        return ScriptProject.objects.create(
            name=f'Gate Project {uuid.uuid4().hex[:8]}',
            key=f'gate-{uuid.uuid4().hex[:8]}',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='scripts',
            **kwargs,
        )

    def test_change_alone_cannot_repoint_a_data_source(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()
        other = DataSource.objects.create(name='Other Repo', type='local', source_url='file:///tmp/other/')

        response = self.client.patch(
            self._get_detail_url(project), {'data_source': other.pk}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('data_source', response.data)
        project.refresh_from_db()
        self.assertNotEqual(project.data_source_id, other.pk)

    def test_change_alone_cannot_move_the_activation_policy(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()

        response = self.client.patch(
            self._get_detail_url(project),
            {'activation_policy': ActivationPolicyChoices.AUTOMATIC_IF_VALID},
            format='json',
            **self.header,
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('activation_policy', response.data)

    def test_activate_permits_the_same_write(self):
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.activate_scriptproject',
        )
        project = self.synchronized()

        response = self.client.patch(
            self._get_detail_url(project), {'data_path': 'automation'}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'automation')

    def racing(self, project, **moved):
        """Patch the serializer so one authorized move lands between validation and the write."""
        # The exact window the gate cannot cover: it compared against the stored row, and by the
        # time anything is written that row has moved. The simulated move shares this request's
        # transaction, so the refusal rolls it back too. What the assertions can show is that the
        # request is refused and writes nothing, which is the contract either way.
        original = ScriptProjectSerializer.save

        def save(inner_self, **kwargs):
            ScriptProject.objects.filter(pk=project.pk).update(**moved)
            return original(inner_self, **kwargs)

        return mock.patch.object(ScriptProjectSerializer, 'save', save)

    def test_a_description_only_patch_does_not_restore_a_stale_source(self):
        # The request submits no gated field at all, so the gate has nothing to compare and the
        # save is a full one. Without the write-time check it writes back the loaded data_path.
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()

        with self.racing(project, data_path='automation'):
            response = self.client.patch(
                self._get_detail_url(project), {'description': 'A note'}, format='json', **self.header
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        project.refresh_from_db()
        self.assertNotEqual(project.description, 'A note')

    def test_resubmitting_an_unchanged_source_value_does_not_undo_a_move(self):
        # Submitted and stored agree when the gate reads them, so no activate is asked for. The
        # authorized move then lands, and this would write the caller's stale value over it.
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()

        with self.racing(project, activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID):
            response = self.client.patch(
                self._get_detail_url(project),
                {'data_path': project.data_path, 'description': 'A note'},
                format='json',
                **self.header,
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        project.refresh_from_db()
        self.assertNotEqual(project.description, 'A note')

    def test_a_permitted_move_still_writes_when_nothing_else_moved(self):
        self.add_permissions(
            'netbox_scripts.view_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.activate_scriptproject',
        )
        project = self.synchronized()

        response = self.client.patch(
            self._get_detail_url(project), {'data_path': 'automation'}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'automation')

    def grant_activate_on(self, project):
        """Grant activate constrained to one Project."""
        permission = ObjectPermission(
            name=f'activate {project.key}', actions=['activate'], constraints={'key': project.key}
        )
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(ScriptProject))

    def test_activate_on_another_project_does_not_permit_the_move(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()
        self.grant_activate_on(self.synchronized())

        response = self.client.patch(
            self._get_detail_url(project), {'data_path': 'automation'}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('data_path', response.data)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'scripts')

    def test_activate_constrained_to_this_project_permits_the_move(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()
        self.grant_activate_on(project)

        response = self.client.patch(
            self._get_detail_url(project), {'data_path': 'automation'}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'automation')

    def test_a_write_that_moves_nothing_is_permitted(self):
        """Submitting the stored value is not a move, so change alone still succeeds."""
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()

        response = self.client.patch(
            self._get_detail_url(project),
            {'data_path': project.data_path, 'description': 'renamed only'},
            format='json',
            **self.header,
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_trailing_separator_is_not_a_move(self):
        """validate_data_path canonicalizes first, so the gate never sees a raw spelling here."""
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.change_scriptproject')
        project = self.synchronized()

        response = self.client.patch(
            self._get_detail_url(project), {'data_path': 'scripts/'}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_create_is_not_gated(self):
        """validate() runs on create too, where there is no stored row to move away from."""
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.add_scriptproject')
        source = DataSource.objects.create(name='Create Repo', type='local', source_url='file:///tmp/c/')

        response = self.client.post(
            self._get_list_url(),
            {
                'name': 'Created Gated',
                'key': 'created-gated',
                'source_type': ProjectSourceTypeChoices.DATA_SOURCE,
                'data_source': source.pk,
                'data_path': 'scripts',
                'activation_policy': ActivationPolicyChoices.AUTOMATIC_IF_VALID,
            },
            format='json',
            **self.header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_key_is_immutable(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.create(name='API Project 1', key='api-project-1')
        response = self.client.patch(
            self._get_detail_url(project), {'key': 'api-project-1-renamed'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('key', response.data)

    def test_source_type_is_immutable(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.create(name='API Project 2', key='api-project-2')
        response = self.client.patch(
            self._get_detail_url(project),
            {'source_type': ProjectSourceTypeChoices.DATA_SOURCE},
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_type', response.data)

    def test_data_path_rejects_traversal(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.create(name='API Project 3', key='api-project-3')
        response = self.client.patch(
            self._get_detail_url(project), {'data_path': '../outside'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('data_path', response.data)

    def test_storage_key_is_read_only(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.create(name='API Project 4', key='api-project-4')
        original = project.storage_key
        response = self.client.patch(
            self._get_detail_url(project), {'storage_key': str(uuid.uuid4())}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.storage_key, original)

    def test_data_path_is_canonicalized(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        data_source = DataSource.objects.create(
            name='API Data Source 1',
            type='local',
            source_url='file:///tmp/api-data-source-1/',
        )
        project = ScriptProject.objects.create(
            name='API Project 5',
            key='api-project-5',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=data_source,
            data_path='automation/netbox',
        )
        response = self.client.patch(
            self._get_detail_url(project), {'data_path': './automation//netbox/'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project.refresh_from_db()
        self.assertEqual(project.data_path, 'automation/netbox')

    def test_active_revision_not_in_response(self):
        self.add_permissions('netbox_scripts.view_scriptproject')
        project = ScriptProject.objects.create(name='API Project 6', key='api-project-6')
        response = self.client.get(self._get_detail_url(project), **self.header)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn('active_revision', response.data)
