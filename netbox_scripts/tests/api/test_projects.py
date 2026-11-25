import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from core.choices import JobStatusChoices
from core.models import DataSource, Job, ObjectChange, ObjectType
from netbox_scripts.activation import activate_revision
from netbox_scripts.api.serializers import ScriptProjectSerializer
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.constants import VALIDATION_LEASE_SECONDS
from netbox_scripts.ingestion import queue_revision_processing
from netbox_scripts.jobs import RevisionValidationJob
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, service, store
from netbox_scripts.storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from netbox_scripts.storage.locks import project_lock
from netbox_scripts.storage.paths import revision_prefix
from netbox_scripts.tests.plugin_testing import (
    IN_MEMORY_STORAGES,
    PluginAPIViewTestCases,
    discard_tree,
)
from netbox_scripts.tests.test_activation import record
from netbox_scripts.tests.test_ingestion import SCRIPT
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

    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))

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

    def staged(self, project, revision_status=None, records=()):
        """Stage distinct content, then give the revision a status and recorded Scripts."""
        revision = service.stage_revision(project, {'deploy.py': f'VALUE = {uuid.uuid4().hex!r}\n'.encode()}).revision
        if revision_status is not None:
            ScriptProjectRevision.objects.filter(pk=revision.pk).update(
                status=revision_status, discovered_scripts=list(records)
            )
            revision.refresh_from_db()
        return revision

    def activated(self, project, records=None):
        """Put a revision with recorded Scripts in service the way the service does."""
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()] if records is None else records)
        activate_revision(revision)
        revision.refresh_from_db()
        return revision

    def post_action(self, project, action, data=None):
        """POST to one of the Project's REST actions with this test's token."""
        url = reverse(f'{self._get_view_namespace()}:scriptproject-{action}', kwargs={'pk': project.pk})
        return self.client.post(url, data or {}, format='json', **self.header)

    def private_runtime_cache(self):
        """Give validation a runtime cache root of this test's own."""
        root = Path(tempfile.mkdtemp(prefix='nbcs-lifecycle-api-'))
        # The runtime tier refuses a cache directory that other users can write.
        root.chmod(0o700)
        self.addCleanup(discard_tree, root)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))

    def run_queued(self, response):
        """Run the Job a response queued and return it."""
        job = Job.objects.get(pk=response.data['id'])
        RevisionValidationJob.handle(job, **job.data)
        return job

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
        # Holds the window open on purpose. One caveat covering every assertion below: the
        # simulated update runs on this connection, so a rollback takes it along. A 400 and an
        # absent description are what separate the fix from its absence.
        original = ScriptProjectSerializer.save

        def save(inner_self, **kwargs):
            ScriptProject.objects.filter(pk=project.pk).update(**moved)
            return original(inner_self, **kwargs)

        return mock.patch.object(ScriptProjectSerializer, 'save', save)

    def test_a_description_only_patch_does_not_restore_a_stale_source(self):
        # A PATCH naming none of the three fields. The narrowest reading of the defect, and the
        # one an operator reaches by accident rather than by trying.
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

    def test_a_data_source_project_created_without_a_path_takes_the_root(self):
        self.add_permissions('netbox_scripts.view_scriptproject', 'netbox_scripts.add_scriptproject')
        source = DataSource.objects.create(name='Root Repo', type='local', source_url='file:///tmp/root/')

        response = self.client.post(
            self._get_list_url(),
            {
                'name': 'Whole Repo',
                'key': 'whole-repo',
                'source_type': ProjectSourceTypeChoices.DATA_SOURCE,
                'data_source': source.pk,
            },
            format='json',
            **self.header,
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ScriptProject.objects.get(key='whole-repo').data_path, '')

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

    def test_reconcile_queues_the_reconciliation_job(self):
        self.add_permissions('netbox_scripts.reconcile_scriptproject')
        project = self.synchronized()

        response = self.post_action(project, 'reconcile')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Script Project source reconciliation')
        self.assertEqual(response.data['data'], {'project_id': project.pk})
        self.assertTrue(response.data['url'].endswith(f'/api/core/jobs/{response.data["id"]}/'))
        job = Job.objects.get(pk=response.data['id'])
        self.assertEqual((job.status, job.data), (JobStatusChoices.STATUS_PENDING, {'project_id': project.pk}))

    def test_reconcile_refuses_an_uploaded_project(self):
        self.add_permissions('netbox_scripts.reconcile_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        jobs = Job.objects.count()

        response = self.post_action(project, 'reconcile')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            str(response.data['detail']),
            'Only projects backed by a Data Source can be reconciled. "ScriptProject 1" holds uploaded files.',
        )
        self.assertEqual(Job.objects.count(), jobs)

    def test_reconcile_needs_the_reconcile_permission(self):
        self.add_permissions(
            'netbox_scripts.add_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.activate_scriptproject',
        )
        project = self.synchronized()
        jobs = Job.objects.count()

        response = self.post_action(project, 'reconcile')

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Job.objects.count(), jobs)

    def test_reconcile_respects_the_grant_constraints(self):
        project, other = self.synchronized(), self.synchronized()
        permission = ObjectPermission(name='reconcile one', actions=['reconcile'], constraints={'key': other.key})
        permission.save()
        permission.users.add(self.user)
        permission.object_types.add(ObjectType.objects.get_for_model(ScriptProject))
        jobs = Job.objects.count()

        response = self.post_action(project, 'reconcile')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(Job.objects.count(), jobs)
        self.assertEqual(self.post_action(other, 'reconcile').status_code, status.HTTP_201_CREATED)

    def test_validate_queues_validation_of_a_materialized_revision(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project)

        response = self.post_action(project, 'validate', {'revision': revision.pk})

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Revision validation')
        self.assertEqual(response.data['data'], {'revision_pk': revision.pk})
        self.assertEqual(Job.objects.get(pk=response.data['id']).data, {'revision_pk': revision.pk})

    def test_validate_takes_over_an_expired_lease_and_refuses_a_live_one(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        expired, live = self.staged(project), self.staged(project)
        ScriptProjectRevision.objects.filter(pk=expired.pk).update(
            status=RevisionStatusChoices.VALIDATING,
            validation_started=timezone.now() - timedelta(seconds=VALIDATION_LEASE_SECONDS + 60),
        )
        ScriptProjectRevision.objects.filter(pk=live.pk).update(
            status=RevisionStatusChoices.VALIDATING, validation_started=timezone.now()
        )
        jobs = Job.objects.count()

        taken = self.post_action(project, 'validate', {'revision': expired.pk})
        refused = self.post_action(project, 'validate', {'revision': live.pk})

        self.assertEqual(taken.status_code, status.HTTP_201_CREATED)
        self.assertEqual(refused.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            refused.data['detail'],
            f'Revision {live.pk} has status "validating" and cannot be queued for validation.',
        )
        self.assertEqual(Job.objects.count(), jobs + 1)
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_validate_refuses_a_revision_that_is_not_waiting_for_a_verdict(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        active = self.activated(project)
        settled = [
            self.staged(project, choice)
            for choice in (
                RevisionStatusChoices.VALID,
                RevisionStatusChoices.INVALID,
                RevisionStatusChoices.RETIRED,
                RevisionStatusChoices.STORAGE_FAILED,
                RevisionStatusChoices.STAGING,
            )
        ]
        jobs = Job.objects.count()

        for revision in [*settled, active]:
            with self.subTest(status=revision.status):
                response = self.post_action(project, 'validate', {'revision': revision.pk})
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertIn(f'has status "{revision.status}"', response.data['detail'])
        with self.subTest('the accepted source of an automatic Project'):
            ScriptProject.objects.filter(pk=project.pk).update(
                activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID
            )
            accepted = self.staged(project, RevisionStatusChoices.VALID, [record()])
            response = self.post_action(project, 'validate', {'revision': accepted.pk})
            self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(Job.objects.count(), jobs)
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, active.pk)

    def test_validate_refuses_a_missing_or_foreign_revision(self):
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        foreign = self.staged(ScriptProject.objects.get(key='project-2'))
        jobs = Job.objects.count()

        for body in ({}, {'revision': 'x'}, {'revision': 0}, {'revision': foreign.pk}):
            with self.subTest(body=body):
                response = self.post_action(project, 'validate', body)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('revision', response.data)
        self.assertEqual(Job.objects.count(), jobs)

    def test_validate_needs_the_change_permission(self):
        self.add_permissions(
            'netbox_scripts.add_scriptproject',
            'netbox_scripts.reconcile_scriptproject',
            'netbox_scripts.activate_scriptproject',
        )
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project)
        jobs = Job.objects.count()

        response = self.post_action(project, 'validate', {'revision': revision.pk})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Job.objects.count(), jobs)

    def test_activate_puts_a_validated_revision_in_service(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(
            project, RevisionStatusChoices.VALID, [record(), record(class_name='AuditDevices', position=1)]
        )

        response = self.post_action(project, 'activate', {'revision': revision.pk})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['revision']['id'], revision.pk)
        self.assertEqual(response.data['revision']['status']['value'], RevisionStatusChoices.ACTIVE)
        self.assertEqual(response.data['scripts'], {'published': 2, 'retired': 0})
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, revision.pk)
        self.assertEqual(NetBoxScript.objects.filter(project=project, is_retired=False).count(), 2)

    def test_activate_returns_to_a_retired_revision(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        first = self.activated(project)
        second = self.staged(project, RevisionStatusChoices.VALID, [record(class_name='AuditDevices')])
        self.assertEqual(self.post_action(project, 'activate', {'revision': second.pk}).status_code, status.HTTP_200_OK)

        response = self.post_action(project, 'activate', {'revision': first.pk})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['scripts'], {'published': 1, 'retired': 1})
        project.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(project.active_revision_id, first.pk)
        self.assertEqual(second.status, RevisionStatusChoices.RETIRED)

    def test_activate_refuses_the_revision_in_service(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        active = self.activated(project)
        changes = ObjectChange.objects.count()

        response = self.post_action(project, 'activate', {'revision': active.pk})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['detail'], f'Revision {active.pk} has status "active" and cannot be activated.')
        self.assertEqual(ObjectChange.objects.count(), changes)
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, active.pk)

    def test_a_revision_activated_while_the_request_waits_for_the_lock_is_refused(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        candidate = self.staged(project, RevisionStatusChoices.VALID, [record()])

        def overtaken(storage_key, *, using):
            activate_revision(candidate)
            return project_lock(storage_key, using=using)

        with mock.patch('netbox_scripts.api.views.project_lock', overtaken):
            response = self.post_action(project, 'activate', {'revision': candidate.pk})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(
            response.data['detail'], f'Revision {candidate.pk} has status "active" and cannot be activated.'
        )

    def test_activate_refuses_a_revision_without_a_usable_verdict(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        active = self.activated(project)

        for choice in (
            RevisionStatusChoices.MATERIALIZED,
            RevisionStatusChoices.VALIDATING,
            RevisionStatusChoices.INVALID,
            RevisionStatusChoices.STORAGE_FAILED,
            RevisionStatusChoices.STAGING,
        ):
            with self.subTest(status=choice):
                revision = self.staged(project, choice)
                with self.assertLogs('netbox.plugins.netbox_scripts.api', 'WARNING') as logs:
                    response = self.post_action(project, 'activate', {'revision': revision.pk})
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(response.data['detail'], 'The revision cannot be activated.')
                self.assertIn(f'has status "{choice}"', logs.output[0])
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, active.pk)

    def test_a_refused_activation_keeps_the_active_revision_in_service(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        active = self.activated(project)
        candidate = self.staged(project, RevisionStatusChoices.VALID, [record(class_name='AuditDevices')])
        storage = config.get_storage()
        key = f'{revision_prefix(project.storage_key, candidate.digest)}deploy.py'
        storage.delete(key)
        storage.save(key, ContentFile(b'TAMPERED = True\n'))

        with self.assertLogs('netbox.plugins.netbox_scripts.api', 'WARNING') as logs:
            response = self.post_action(project, 'activate', {'revision': candidate.pk})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data['detail'], 'The revision cannot be activated.')
        self.assertNotIn('deploy.py', str(response.data))
        self.assertIn('deploy.py', logs.output[0])
        project.refresh_from_db()
        candidate.refresh_from_db()
        self.assertEqual(project.active_revision_id, active.pk)
        self.assertEqual(candidate.status, RevisionStatusChoices.VALID)
        self.assertFalse(NetBoxScript.objects.get(project=project, class_name='DeployDevices').is_retired)

    def test_a_storage_failure_returns_a_fixed_message_and_logs_its_cause(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()])

        for failure in (StorageError('/srv/private unreachable'), PermissionError('/srv/private')):
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(store, 'verify_revision_tree', side_effect=failure),
                    self.assertLogs('netbox.plugins.netbox_scripts.api', 'ERROR'),
                ):
                    response = self.post_action(project, 'activate', {'revision': revision.pk})
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertEqual(str(response.data['detail']), 'The revision could not be activated.')
                self.assertNotIn('private', str(response.data))
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_a_refused_activation_returns_a_fixed_message_and_logs_its_reason(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()])

        for failure in (ActivationError('/srv/private refused'), RevisionCorruptError('/srv/private damaged')):
            with self.subTest(failure=type(failure).__name__):
                with (
                    mock.patch.object(store, 'verify_revision_tree', side_effect=failure),
                    self.assertLogs('netbox.plugins.netbox_scripts.api', 'WARNING') as logs,
                ):
                    response = self.post_action(project, 'activate', {'revision': revision.pk})
                self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
                self.assertEqual(response.data['detail'], 'The revision cannot be activated.')
                self.assertNotIn('private', str(response.data))
                self.assertIn('private', logs.output[0])
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_activate_refuses_a_missing_or_foreign_revision(self):
        self.add_permissions('netbox_scripts.activate_scriptproject')
        project, other = ScriptProject.objects.get(key='project-1'), ScriptProject.objects.get(key='project-2')
        foreign = self.staged(other, RevisionStatusChoices.VALID, [record()])

        for body in ({}, {'revision': 'x'}, {'revision': foreign.pk}):
            with self.subTest(body=body):
                response = self.post_action(project, 'activate', body)
                self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                self.assertIn('revision', response.data)
        self.assertFalse(
            ScriptProject.objects.filter(pk__in=(project.pk, other.pk), active_revision__isnull=False).exists()
        )

    def test_activate_needs_the_activate_permission(self):
        self.add_permissions('netbox_scripts.add_scriptproject', 'netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()])

        response = self.post_action(project, 'activate', {'revision': revision.pk})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_activate_respects_the_grant_constraints(self):
        project = ScriptProject.objects.get(key='project-1')
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()])
        self.grant_activate_on(ScriptProject.objects.get(key='project-2'))

        response = self.post_action(project, 'activate', {'revision': revision.pk})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)
        self.grant_activate_on(project)
        self.assertEqual(
            self.post_action(project, 'activate', {'revision': revision.pk}).status_code, status.HTTP_200_OK
        )

    def test_a_read_only_token_cannot_reconcile_validate_or_activate(self):
        self.add_permissions(
            'netbox_scripts.reconcile_scriptproject',
            'netbox_scripts.change_scriptproject',
            'netbox_scripts.activate_scriptproject',
        )
        self.token.write_enabled = False
        self.token.save()
        project = self.synchronized()
        revision = self.staged(project, RevisionStatusChoices.VALID, [record()])
        jobs = Job.objects.count()

        for action in ('reconcile', 'validate', 'activate'):
            with self.subTest(action=action):
                response = self.post_action(project, action, {'revision': revision.pk})
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Job.objects.count(), jobs)
        project.refresh_from_db()
        self.assertIsNone(project.active_revision_id)

    def test_a_failed_validation_queued_over_rest_keeps_the_active_revision(self):
        self.private_runtime_cache()
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        ScriptProject.objects.filter(pk=project.pk).update(activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        ScriptFile.objects.create(project=project, source_path='deploy.py')
        good = service.stage_revision(project, {'deploy.py': SCRIPT}).revision
        self.run_queued(self.post_action(project, 'validate', {'revision': good.pk}))
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, good.pk)
        broken = service.stage_revision(project, {'deploy.py': b'def broken(:\n'}).revision

        self.run_queued(self.post_action(project, 'validate', {'revision': broken.pk}))

        broken.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(broken.status, RevisionStatusChoices.INVALID)
        self.assertEqual(project.active_revision_id, good.pk)
        self.assertFalse(NetBoxScript.objects.get(project=project, class_name='Deploy').is_retired)

    def test_validation_queued_over_rest_leaves_a_manual_project_waiting(self):
        self.private_runtime_cache()
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        ScriptFile.objects.create(project=project, source_path='deploy.py')
        active = self.activated(project)
        revision = service.stage_revision(project, {'deploy.py': SCRIPT}).revision

        response = self.post_action(project, 'validate', {'revision': revision.pk})

        project.refresh_from_db()
        self.assertFalse(project.source_activation_pending)
        self.run_queued(response)
        revision.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        self.assertEqual(project.active_revision_id, active.pk)

    def test_a_one_shot_activation_request_survives_validation_queued_over_rest(self):
        self.private_runtime_cache()
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        ScriptFile.objects.create(project=project, source_path='deploy.py')
        revision = service.stage_revision(project, {'deploy.py': SCRIPT}).revision
        ScriptProject.objects.filter(pk=project.pk).update(source_activation_pending=True)

        response = self.post_action(project, 'validate', {'revision': revision.pk})

        project.refresh_from_db()
        self.assertTrue(project.source_activation_pending)
        self.run_queued(response)
        project.refresh_from_db()
        self.assertEqual(project.active_revision_id, revision.pk)

    def test_a_superseded_candidate_validated_over_rest_is_not_activated(self):
        self.private_runtime_cache()
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        ScriptProject.objects.filter(pk=project.pk).update(activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID)
        ScriptFile.objects.create(project=project, source_path='deploy.py')
        candidate = service.stage_revision(project, {'deploy.py': SCRIPT}).revision
        accepted = service.stage_revision(project, {'deploy.py': SCRIPT + b'\nVERSION = 2\n'}).revision

        self.run_queued(self.post_action(project, 'validate', {'revision': candidate.pk}))

        candidate.refresh_from_db()
        project.refresh_from_db()
        self.assertEqual(candidate.status, RevisionStatusChoices.VALID)
        self.assertIsNone(project.active_revision_id)
        self.assertEqual(project.source_revision_id, accepted.pk)

    def test_a_validation_queued_beside_a_pending_job_reports_the_same_verdict(self):
        self.private_runtime_cache()
        self.add_permissions('netbox_scripts.change_scriptproject')
        project = ScriptProject.objects.get(key='project-1')
        ScriptFile.objects.create(project=project, source_path='deploy.py')

        for source, verdict, messages in (
            (
                SCRIPT,
                RevisionStatusChoices.VALID,
                [
                    'The revision validated as valid.',
                    'Leaving the active revision unchanged. This source is superseded or needs manual approval.',
                ],
            ),
            (b'def broken(:\n', RevisionStatusChoices.INVALID, ['The revision is invalid, 1 problem(s) recorded.']),
        ):
            with self.subTest(verdict=verdict):
                revision = service.stage_revision(project, {'deploy.py': source}).revision
                pending = queue_revision_processing(revision)
                response = self.post_action(project, 'validate', {'revision': revision.pk})
                RevisionValidationJob.handle(pending, **pending.data)
                queued = self.run_queued(response)

                self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                revision.refresh_from_db()
                self.assertEqual(revision.status, verdict)
                self.assertEqual(revision.validation_job_id, pending.pk)
                for job in (pending, queued):
                    job.refresh_from_db()
                    self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
                    self.assertEqual([entry['message'] for entry in job.log_entries], messages)
