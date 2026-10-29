from unittest import mock

from django.db.models.signals import pre_save
from django.urls import reverse
from rest_framework import status

from core.choices import JobStatusChoices
from core.models import Job
from extras.events import serialize_for_event
from netbox.jobs import AsyncAPIJob
from netbox_scripts.api.serializers import NetBoxScriptSerializer
from netbox_scripts.api.views import ScriptFileViewSet
from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.jobs import ProjectScriptFileRefreshJob
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.permissions import SELECTION_REFUSAL
from netbox_scripts.tests.plugin_testing import (
    ObjectPermissionTestMixin,
    PluginAPIViewTestCase,
    PluginAPIViewTestCases,
    create_scripts_out_of_default_order,
    refresh_count,
)
from utilities.api import get_serializer_for_model
from utilities.testing import APITestCase
from utilities.testing.mixins import RQQueueTestMixin

DIGEST = 'e' * 64


class NetBoxScriptAPIViewTestCase(PluginAPIViewTestCase, APITestCase):
    """The update-only Script endpoint and the identity fields NetBox reverses."""

    model = NetBoxScript

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='API Script Project', key='api-script-project')
        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest=DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        cls.script = NetBoxScript.objects.create(
            project=cls.project,
            module_path='deploy',
            class_name='DeployDevices',
            display_name='Deploy Devices',
            description='Deploy devices at a site.',
            last_seen_revision=cls.revision,
            metadata={'commit_default': True, 'job_timeout': 300},
        )
        NetBoxScript.objects.create(
            project=cls.project,
            module_path='helpers',
            class_name='Retired',
            display_name='Retired',
            is_retired=True,
        )

    def test_the_serializer_is_resolvable_from_the_package_path(self):
        # serialize_for_event() composes this path from the app label and model name, so a
        # package-layout refactor must not move the class out of it.
        self.assertIs(get_serializer_for_model(NetBoxScript), NetBoxScriptSerializer)

    def test_the_identity_fields_resolve_without_a_request(self):
        # url is a reversing field, so it fails outright if no API route is registered.
        data = NetBoxScriptSerializer(self.script, context={'request': None}).data
        self.assertEqual(data['url'], f'/api/plugins/netbox-scripts/scripts/{self.script.pk}/')
        self.assertEqual(data['display'], 'Deploy Devices')
        self.assertEqual(data['project']['id'], self.project.pk)
        # Nested, so the revision's own reversing field resolves through this caller too.
        self.assertEqual(data['last_seen_revision']['id'], self.revision.pk)
        self.assertEqual(
            data['last_seen_revision']['url'],
            f'/api/plugins/netbox-scripts/project-revisions/{self.revision.pk}/',
        )

    def test_serialize_for_event_returns_data(self):
        data = serialize_for_event(self.script)
        self.assertEqual(data['id'], self.script.pk)
        self.assertEqual(data['class_name'], 'DeployDevices')

    def test_deleting_the_project_serializes_the_cascading_script_events(self):
        # Delete events serialize eagerly, unlike create and update, so this path needs the
        # serializer even though activation never runs in this request.
        project = ScriptProject.objects.create(name='Cascade Project', key='cascade-project')
        NetBoxScript.objects.create(
            project=project,
            module_path='deploy',
            class_name='Cascaded',
            display_name='Cascaded',
        )
        self.add_permissions(
            'netbox_scripts.delete_scriptproject',
            'netbox_scripts.delete_netboxscript',
            'netbox_scripts.view_scriptproject',
        )
        self.client.force_login(self.user)

        url = reverse('plugins:netbox_scripts:scriptproject_delete', kwargs={'pk': project.pk})
        response = self.client.post(url, {'confirm': True}, follow=False)

        self.assertIn(response.status_code, (status.HTTP_200_OK, status.HTTP_302_FOUND))
        self.assertFalse(NetBoxScript.objects.filter(project_id=project.pk).exists())
        self.assertFalse(ScriptProject.objects.filter(pk=project.pk).exists())

    def test_list_and_detail_return_200_with_permission(self):
        self.add_permissions('netbox_scripts.view_netboxscript')

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
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.add_netboxscript',
            'netbox_scripts.change_netboxscript',
            'netbox_scripts.delete_netboxscript',
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

        self.assertEqual(NetBoxScript.objects.count(), 2)

    def test_enabled_is_patchable(self):
        self.add_permissions(
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
        )
        response = self.client.patch(
            self._get_detail_url(self.script), {'enabled': False}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.script.refresh_from_db()
        self.assertFalse(self.script.enabled)

    def test_the_execution_overrides_are_patchable(self):
        self.add_permissions(
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
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
        self.add_permissions('netbox_scripts.view_netboxscript')
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
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
        )
        NetBoxScript.objects.filter(pk=self.script.pk).update(notifications_default_override='never')
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
        self.add_permissions('netbox_scripts.view_netboxscript')
        response = self.client.get(self._get_detail_url(self.script), **self.header)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['notifications_default_override'])

    def test_an_override_is_clearable_over_rest(self):
        self.add_permissions(
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
        )
        NetBoxScript.objects.filter(pk=self.script.pk).update(job_timeout_override=45)
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
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
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
        other = ScriptProject.objects.create(name='Other Project', key='other-project')
        self.add_permissions(
            'netbox_scripts.view_netboxscript',
            'netbox_scripts.change_netboxscript',
        )

        response = self.client.patch(
            self._get_detail_url(self.script), {'project': other.pk}, format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.script.refresh_from_db()
        self.assertEqual(self.script.project_id, self.project.pk)

    def test_patching_requires_the_change_permission(self):
        self.add_permissions('netbox_scripts.view_netboxscript')
        response = self.client.patch(
            self._get_detail_url(self.script), {'enabled': False}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_bulk_update_saves_in_primary_key_order(self):
        pks = create_scripts_out_of_default_order()
        saved = []

        def record_save(sender, instance, **kwargs):
            saved.append(instance.pk)

        pre_save.connect(record_save, sender=NetBoxScript, dispatch_uid='bulk-update-save-order')
        self.addCleanup(pre_save.disconnect, record_save, sender=NetBoxScript, dispatch_uid='bulk-update-save-order')
        self.add_permissions('netbox_scripts.view_netboxscript', 'netbox_scripts.change_netboxscript')

        response = self.client.patch(
            self._get_list_url(), [{'id': pk, 'enabled': False} for pk in pks[::-1]], format='json', **self.header
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(saved, pks)


FILE_DIGEST = 'c' * 64


class ScriptFileAPIViewTestCase(PluginAPIViewTestCases.NestedObjectAPIViewTestCase):
    model = ScriptFile
    # The root fields carry the plugin prefix, which the verbose name the mixin derives from does not.
    graphql_base_name = 'netbox_script_file'
    brief_fields = ['description', 'display', 'id', 'source_path', 'url']
    graphql_filter = {'source_path': {'lookup': 'i_contains', 'value': 'tools'}}
    update_data = {
        'description': 'Updated description',
        'enabled': False,
    }
    bulk_update_data = {
        'description': 'Bulk-updated description',
        'enabled': False,
    }
    # Refused by the serializer's BooleanField.
    bulk_update_invalid_data = {'enabled': 'maybe'}

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='API Script File Project', key='api-script-file-project')

        ScriptFile.objects.create(project=cls.project, source_path='tools/first.py', description='First script file')
        ScriptFile.objects.create(project=cls.project, source_path='tools/second.py', description='Second script file')
        ScriptFile.objects.create(project=cls.project, source_path='tools/third.py', enabled=False)

    def setUp(self):
        super().setUp()
        # update_data disables a Script File, which also needs its Project's change permission.
        self.add_permissions('netbox_scripts.change_scriptproject')

    def test_a_patch_does_not_revert_a_discovery_result(self):
        # read_only keeps a submitted value out and settles nothing about the copy the view
        # already holds. Before v4.7.4 core closes this window only when the caller sends If-Match,
        # so the load is patched to reach it and everything past the load stays real.
        self.add_permissions('netbox_scripts.view_scriptfile', 'netbox_scripts.change_scriptfile')
        script_file = ScriptFile.objects.get(source_path='tools/first.py')
        stale = ScriptFile.objects.get(pk=script_file.pk)
        ScriptFile.objects.filter(pk=script_file.pk).update(discovery_status=FileDiscoveryStatusChoices.FAILED)

        with mock.patch.object(ScriptFileViewSet, 'get_object_with_snapshot', return_value=stale):
            response = self.client.patch(
                self._get_detail_url(script_file), {'enabled': False}, format='json', **self.header
            )

        self.assertHttpStatus(response, status.HTTP_200_OK)
        script_file.refresh_from_db()
        self.assertEqual(script_file.discovery_status, FileDiscoveryStatusChoices.FAILED)
        self.assertFalse(script_file.enabled)

    def test_the_list_route_refuses_a_post(self):
        """A declaration is a Project setting, and a nested numeric id is always permitted."""
        self.add_permissions('netbox_scripts.add_scriptfile')

        response = self.client.post(
            self._get_list_url(),
            {'project': self.project.pk, 'source_path': 'tools/smuggled.py'},
            format='json',
            **self.header,
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertFalse(ScriptFile.objects.filter(source_path='tools/smuggled.py').exists())

    def test_the_detail_route_refuses_a_delete(self):
        self.add_permissions('netbox_scripts.delete_scriptfile')
        script_file = ScriptFile.objects.create(project=self.project, source_path='tools/doomed.py')

        response = self.client.delete(self._get_detail_url(script_file), **self.header)

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(ScriptFile.objects.filter(pk=script_file.pk).exists())

    def test_discovery_fields_are_read_only(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        script_file = ScriptFile.objects.create(project=self.project, source_path='tools/system.py')
        revision = ScriptProjectRevision.objects.create(
            project=self.project,
            digest=FILE_DIGEST,
            status=RevisionStatusChoices.MATERIALIZED,
        )
        response = self.client.patch(
            self._get_detail_url(script_file),
            {
                'discovery_status': FileDiscoveryStatusChoices.DISCOVERED,
                'discovery_error': 'Injected error',
                'last_discovered_revision': revision.pk,
            },
            format='json',
            **self.header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        script_file.refresh_from_db()
        self.assertEqual(script_file.discovery_status, FileDiscoveryStatusChoices.PENDING)
        self.assertEqual(script_file.discovery_error, '')
        self.assertIsNone(script_file.last_discovered_revision)

    def test_source_path_is_immutable(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        script_file = ScriptFile.objects.create(project=self.project, source_path='tools/frozen.py')
        response = self.client.patch(
            self._get_detail_url(script_file), {'source_path': 'tools/renamed.py'}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_path', response.data)
        script_file.refresh_from_db()
        self.assertEqual(script_file.source_path, 'tools/frozen.py')

    def test_project_is_immutable(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        other = ScriptProject.objects.create(name='API Other Project', key='api-other-project')
        script_file = ScriptFile.objects.create(project=self.project, source_path='tools/owned.py')
        response = self.client.patch(
            self._get_detail_url(script_file), {'project': other.pk}, format='json', **self.header
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('project', response.data)


class ScriptFileSelectionAPITestCase(ObjectPermissionTestMixin, APITestCase):
    """Enabling or disabling a Script File over REST follows its Project's Script Files tab."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='API Selection Project', key='api-selection-project')
        cls.other = ScriptProject.objects.create(name='API Selection Elsewhere', key='api-selection-elsewhere')
        cls.first = ScriptFile.objects.create(project=cls.project, source_path='first.py')
        cls.second = ScriptFile.objects.create(project=cls.project, source_path='second.py')
        cls.elsewhere = ScriptFile.objects.create(project=cls.other, source_path='elsewhere.py')

    def patch(self, script_file, data):
        url = reverse('plugins-api:netbox_scripts-api:scriptfile-detail', args=[script_file.pk])
        return self.client.patch(url, data, format='json', **self.header)

    def bulk_patch(self, script_files, data):
        url = reverse('plugins-api:netbox_scripts-api:scriptfile-list')
        payload = [{'id': script_file.pk, **data} for script_file in script_files]
        return self.client.patch(url, payload, format='json', **self.header)

    def test_disabling_a_script_file_restages_its_project(self):
        self.add_permissions('netbox_scripts.change_scriptfile', 'netbox_scripts.change_scriptproject')

        response = self.patch(self.first, {'enabled': False})

        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.first.refresh_from_db()
        self.assertFalse(self.first.enabled)
        self.assertEqual(refresh_count(self.project), 1)

    def test_an_edit_that_keeps_the_selection_needs_only_the_script_file_permission(self):
        self.add_permissions('netbox_scripts.change_scriptfile')

        response = self.patch(self.first, {'enabled': True, 'description': 'Edited'})

        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.first.refresh_from_db()
        self.assertEqual(self.first.description, 'Edited')
        self.assertEqual(refresh_count(self.project), 0)

    def test_disabling_needs_the_project_change_permission(self):
        self.add_permissions('netbox_scripts.change_scriptfile')

        response = self.patch(self.first, {'enabled': False})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual(response.data['detail'], str(SELECTION_REFUSAL))
        self.first.refresh_from_db()
        self.assertTrue(self.first.enabled)
        self.assertEqual(refresh_count(self.project), 0)

    def test_a_project_permission_scoped_to_another_project_does_not_count(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        self.grant(ScriptProject, 'change', constraints={'pk': self.other.pk})

        response = self.patch(self.first, {'enabled': False})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.first.refresh_from_db()
        self.assertTrue(self.first.enabled)

    def test_a_bulk_update_restages_each_project_once(self):
        self.add_permissions('netbox_scripts.change_scriptfile', 'netbox_scripts.change_scriptproject')

        response = self.bulk_patch((self.first, self.second, self.elsewhere), {'enabled': False})

        self.assertHttpStatus(response, status.HTTP_200_OK)
        self.assertEqual(refresh_count(self.project), 1)
        self.assertEqual(refresh_count(self.other), 1)

    def test_a_refused_row_rolls_back_the_whole_bulk_update(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        self.grant(ScriptProject, 'change', constraints={'pk': self.project.pk})
        enqueue_refresh = ProjectScriptFileRefreshJob.enqueue_refresh

        with mock.patch.object(ProjectScriptFileRefreshJob, 'enqueue_refresh', wraps=enqueue_refresh) as enqueue:
            response = self.bulk_patch((self.first, self.elsewhere), {'enabled': False})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.assertEqual([entry['id'] for entry in response.data['errors']], [self.elsewhere.pk])
        # The permitted row saved and queued its refresh before the batch rolled back.
        self.assertEqual(enqueue.call_count, 1)
        for script_file in (self.first, self.elsewhere):
            script_file.refresh_from_db()
            self.assertTrue(script_file.enabled)
        self.assertEqual(refresh_count(self.project), 0)

    def test_a_stale_copy_cannot_re_enable_a_script_file(self):
        self.add_permissions('netbox_scripts.change_scriptfile')
        stale = ScriptFile.objects.get(pk=self.first.pk)
        ScriptFile.objects.filter(pk=self.first.pk).update(enabled=False)

        # The view's load is patched to hand back a copy taken before the tab disabled the file.
        with mock.patch.object(ScriptFileViewSet, 'get_object_with_snapshot', return_value=stale):
            response = self.patch(self.first, {'description': 'Edited'})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.first.refresh_from_db()
        self.assertFalse(self.first.enabled)
        self.assertEqual(refresh_count(self.project), 0)

    def test_a_stale_copy_cannot_re_enable_a_script_file_outside_the_change_scope(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        self.grant(ScriptFile, 'change', constraints={'enabled': True})
        stale = ScriptFile.objects.get(pk=self.first.pk)
        ScriptFile.objects.filter(pk=self.first.pk).update(enabled=False)

        # From v4.7.4 a write inside this load would roll back with the refusal.
        with mock.patch.object(ScriptFileViewSet, 'get_object_with_snapshot', return_value=stale):
            response = self.patch(self.first, {'description': 'Edited'})

        self.assertHttpStatus(response, status.HTTP_403_FORBIDDEN)
        self.first.refresh_from_db()
        self.assertFalse(self.first.enabled)
        self.assertEqual(refresh_count(self.project), 0)


class ScriptFileBackgroundSelectionAPITestCase(RQQueueTestMixin, APITestCase):
    """A background bulk update of Script Files follows the Script Files tab's rules too."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='API Background Project', key='api-background-project')
        cls.script_files = [
            ScriptFile.objects.create(project=cls.project, source_path=path) for path in ('first.py', 'second.py')
        ]

    def setUp(self):
        super().setUp()
        enqueue = AsyncAPIJob.enqueue.__func__

        # Inline, as NetBox's own background bulk tests run AsyncAPIJob.
        def enqueue_inline(cls, *args, **kwargs):
            kwargs.setdefault('immediate', True)
            return enqueue(cls, *args, **kwargs)

        self.enterContext(mock.patch.object(AsyncAPIJob, 'enqueue', classmethod(enqueue_inline)))
        self.enterContext(mock.patch('netbox.api.viewsets.mixins.any_workers_for_queue', return_value=True))

    def test_a_background_bulk_update_restages_the_project_once(self):
        self.add_permissions('netbox_scripts.change_scriptfile', 'netbox_scripts.change_scriptproject')
        url = reverse('plugins-api:netbox_scripts-api:scriptfile-list')
        payload = [{'id': script_file.pk, 'enabled': False} for script_file in self.script_files]

        response = self.client.patch(f'{url}?background=true', payload, format='json', **self.header)

        self.assertHttpStatus(response, status.HTTP_202_ACCEPTED)
        self.assertEqual(Job.objects.get(pk=response.data['job']['id']).status, JobStatusChoices.STATUS_COMPLETED)
        self.assertEqual(refresh_count(self.project), 1)
