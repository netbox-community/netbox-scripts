"""
Lock ordering for the REST write paths and the Project bulk edit.

Every statement that locks or writes a target row is observed from a second session, which must find the owning
Projects' locks already taken. A probe at save() runs after NetBox's own row lock, so it cannot see the order.

TransactionTestCase, and not only because a second session needs committed rows. Under TestCase
the project built in setUp would hold its pg_advisory_xact_lock until teardown, since that lock
lives until the outermost transaction ends, so every probe would read the lock as held and the
negative control could never fail.
"""

import uuid
from contextlib import contextmanager
from unittest import mock

from django.db import DatabaseError, connection, transaction
from django.test import Client, TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_scripts.models import ScriptFile, ScriptProject
from netbox_scripts.storage.locks import advisory_key, project_lock
from netbox_scripts.tests.plugin_testing import IN_MEMORY_STORAGES, ObjectPermissionTestMixin, SecondSession
from users.constants import TOKEN_PREFIX
from users.models import Token, User
from utilities.testing import post_data


class WriteLockOrderTestCase(ObjectPermissionTestMixin, TransactionTestCase):
    """REST writes and the Project bulk edit take the owning Projects' locks before NetBox locks or writes a row."""

    client_class = APIClient

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.user = User.objects.create_user(username='testuser')
        self.token = Token.objects.create(user=self.user)
        # A v2 token authenticates as prefix, key and secret together, never the key alone.
        self.header = {'HTTP_AUTHORIZATION': f'Bearer {TOKEN_PREFIX}{self.token.key}.{self.token.token}'}
        self.grant(ScriptProject, 'view', 'change', 'delete')
        self.grant(ScriptFile, 'view', 'change', 'delete')
        self.script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')

    def url(self, instance, *, bulk=False):
        """Return the REST detail route for one object, or its model's list route."""
        if bulk:
            return reverse(f'plugins-api:netbox_scripts-api:{instance._meta.model_name}-list')
        return reverse(f'plugins-api:netbox_scripts-api:{instance._meta.model_name}-detail', kwargs={'pk': instance.pk})

    def etag(self, instance):
        """Return the ETag a client would have been served for one object."""
        response = self.client.get(self.url(instance), **self.header)
        self.assertEqual(response.status_code, 200)
        return response['ETag']

    @staticmethod
    def update_data(instance):
        """Return an edit that a PUT accepts as well as a PATCH."""
        if isinstance(instance, ScriptProject):
            return {'description': 'edited', 'name': instance.name, 'key': instance.key}
        return {'description': 'edited', 'project': instance.project_id, 'source_path': instance.source_path}

    def assert_locks_released(self, projects):
        """Fail unless another session can take each of these Projects' locks."""
        with SecondSession() as other:
            for project in projects:
                self.assertTrue(other.can_lock(project.storage_key), 'A Project lock was not released.')

    @contextmanager
    def observe_row_writes(self, model, projects):
        """Fail when a statement locks or writes one of the model's rows before every Project lock is held."""
        table = connection.ops.quote_name(model._meta.db_table)
        observed = []

        def observe(execute, sql, params, many, context):
            locks_row = 'FOR UPDATE' in sql or 'FOR NO KEY UPDATE' in sql
            if table in sql and (locks_row or sql.startswith((f'UPDATE {table}', f'DELETE FROM {table}'))):
                with SecondSession() as other:
                    for project in projects:
                        self.assertFalse(other.can_lock(project.storage_key), 'A row lock preceded its Project lock.')
                observed.append(sql)
            return execute(sql, params, many, context)

        self.assert_locks_released(projects)
        with connection.execute_wrapper(observe):
            yield
        self.assertTrue(observed, 'The request neither locked nor wrote a row.')
        self.assert_locks_released(projects)

    def test_single_updates_lock_the_project_before_the_row(self):
        for instance in (self.project, self.script_file):
            for method in ('patch', 'put'):
                for conditional in (False, True):
                    with self.subTest(model=type(instance).__name__, method=method, conditional=conditional):
                        headers = {'HTTP_IF_MATCH': self.etag(instance)} if conditional else {}
                        with self.observe_row_writes(type(instance), (self.project,)):
                            response = getattr(self.client, method)(
                                self.url(instance), self.update_data(instance), format='json', **headers, **self.header
                            )

                        self.assertEqual(response.status_code, 200, response.content)

    def test_bulk_updates_lock_all_projects_before_any_row(self):
        # Name order is the reverse of pk, key, storage-key and advisory-key order, so only name order passes.
        keys = sorted((uuid.UUID(int=2), uuid.UUID(int=3)), key=advisory_key)
        self.assertEqual(keys, sorted(keys))
        zulu = ScriptProject.objects.create(name='Zulu Devices', key='devices-1', storage_key=keys[0])
        alpha = ScriptProject.objects.create(name='Alpha Devices', key='devices-2', storage_key=keys[1])
        files = [
            ScriptFile.objects.create(project=project, source_path=path)
            for project, path in ((zulu, 'deploy.py'), (alpha, 'audit.py'), (zulu, 'other.py'))
        ]

        for instances in ((zulu, alpha), files):
            model = type(instances[0])
            data = [{'id': instance.pk, **self.update_data(instance)} for instance in instances]
            for method in ('patch', 'put'):
                for conditional in (False, True):
                    with self.subTest(model=model.__name__, method=method, conditional=conditional):
                        headers = {}
                        if conditional:
                            headers['HTTP_IF_MATCH'] = ', '.join(self.etag(instance) for instance in instances)
                        with (
                            mock.patch('netbox_scripts.api.views.project_lock', wraps=project_lock) as acquire,
                            self.observe_row_writes(model, (zulu, alpha)),
                        ):
                            response = getattr(self.client, method)(
                                self.url(instances[0], bulk=True), data, format='json', **headers, **self.header
                            )

                        self.assertEqual(response.status_code, 200, response.content)
                        self.assertEqual(
                            [call.args[0] for call in acquire.call_args_list], [alpha.storage_key, zulu.storage_key]
                        )
                    data.reverse()

    def test_a_project_bulk_edit_holds_every_lock_through_its_writes(self):
        bravo = ScriptProject.objects.create(name='Bravo Devices', key='bravo-devices')
        client = Client()
        client.force_login(self.user)
        data = {'pk': [self.project.pk, bravo.pk], '_apply': True, **post_data({'description': 'edited'})}

        with self.observe_row_writes(ScriptProject, (self.project, bravo)):
            response = client.post(reverse('plugins:netbox_scripts:scriptproject_bulk_edit'), data)

        self.assertEqual(response.status_code, 302)

    def test_a_conditional_project_delete_holds_the_project_lock_first(self):
        etag = self.etag(self.project)
        # NetBox's conditional row lock precedes the cascade's write lock, so only the mixin's lock passes.
        with self.observe_row_writes(ScriptProject, (self.project,)):
            response = self.client.delete(self.url(self.project), HTTP_IF_MATCH=etag, **self.header)

        self.assertEqual(response.status_code, 204)

    def test_the_probe_detects_a_row_lock_without_a_project_lock(self):
        with (
            self.assertRaisesRegex(AssertionError, 'A row lock preceded its Project lock'),
            self.observe_row_writes(ScriptProject, (self.project,)),
            transaction.atomic(),
        ):
            list(ScriptProject.objects.filter(pk=self.project.pk).select_for_update())

    def test_a_stale_etag_is_refused_and_releases_the_lock(self):
        for instance in (self.project, self.script_file):
            with self.subTest(model=type(instance).__name__):
                response = self.client.patch(
                    self.url(instance),
                    {'description': 'edited'},
                    format='json',
                    HTTP_IF_MATCH='"not-the-current-etag"',
                    **self.header,
                )

                self.assertEqual(response.status_code, 412)
                instance.refresh_from_db()
                self.assertEqual(instance.description, '')
                self.assert_locks_released((self.project,))

    def test_a_database_error_releases_the_lock_after_the_rollback(self):
        def fail(instance):
            # Aborts update()'s transaction, outside a savepoint, so only an unlock after rollback works.
            with connection.cursor() as cursor:
                cursor.execute('SELECT 1 / 0')

        with mock.patch.object(ScriptProject, 'snapshot', fail), self.assertRaises(DatabaseError):
            self.client.patch(self.url(self.project), {'description': 'edited'}, format='json', **self.header)

        self.assert_locks_released((self.project,))

    def test_missing_and_invalid_detail_ids_are_not_found(self):
        for instance in (self.project, self.script_file):
            for pk in (0, 'not-an-id'):
                with (
                    self.subTest(model=type(instance).__name__, pk=pk),
                    mock.patch('netbox_scripts.api.views.project_lock', wraps=project_lock) as acquire,
                ):
                    response = self.client.patch(
                        f'{self.url(instance, bulk=True)}{pk}/', {'description': 'edited'}, format='json', **self.header
                    )

                    self.assertEqual(response.status_code, 404)
                    acquire.assert_not_called()
                    self.assert_locks_released((self.project,))
