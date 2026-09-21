"""
Lock ordering for the REST write paths.

NetBox takes a row lock before serializer.save() on a conditional request, and the models these
viewsets write take the project lock in their own save, which is the order activation takes the
two in reversed. These prove the mixin closes that by taking the project lock first.

TransactionTestCase, and not only because a second session needs committed rows. Under TestCase
the project built in setUp would hold its pg_advisory_xact_lock until teardown, since that lock
lives until the outermost transaction ends, so every probe would read the lock as held and the
negative control could never fail.
"""

from unittest import mock

import django_rq
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient

from netbox_scripts.models import ScriptFile, ScriptProject
from netbox_scripts.tests.plugin_testing import IN_MEMORY_STORAGES, ObjectPermissionTestMixin, SecondSession
from users.constants import TOKEN_PREFIX
from users.models import Token, User


class ConditionalWriteLockOrderTestCase(ObjectPermissionTestMixin, TransactionTestCase):
    """A conditional REST write holds the project lock before core takes the row lock."""

    client_class = APIClient

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        # This case commits, so a deletion really enqueues its cleanup. The queue is isolated by
        # testing/configuration.py, and draining it leaves nothing behind even there.
        self.addCleanup(django_rq.get_queue('default').empty)
        self.project = ScriptProject.objects.create(name='Deploy Devices', key='deploy-devices')
        self.user = User.objects.create_user(username='testuser')
        self.token = Token.objects.create(user=self.user)
        # A v2 token authenticates as prefix, key and secret together, never the key alone.
        self.header = {'HTTP_AUTHORIZATION': f'Bearer {TOKEN_PREFIX}{self.token.key}.{self.token.token}'}
        self.grant(ScriptProject, 'view', 'change', 'delete')
        self.grant(ScriptFile, 'view', 'change', 'delete')
        self.script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')

    def url(self, instance):
        """Return the REST detail route for one object."""
        return reverse(f'plugins-api:netbox_scripts-api:{instance._meta.model_name}-detail', kwargs={'pk': instance.pk})

    def etag(self, instance):
        """Return the ETag a client would have been served for one object."""
        response = self.client.get(self.url(instance), **self.header)
        self.assertEqual(response.status_code, 200)
        return response['ETag']

    def observe_project_lock_at(self, model, attribute, request):
        """Run a request and report whether the project lock was held when the model's write began."""
        observed = {}
        real = getattr(model, attribute)

        def observe(instance, *args, **kwargs):
            with SecondSession() as other:
                observed['held'] = not other.can_lock(self.project.storage_key)
            return real(instance, *args, **kwargs)

        with mock.patch.object(model, attribute, observe):
            response = request()
        return observed.get('held'), response

    def test_a_conditional_project_update_holds_the_project_lock_first(self):
        held, response = self.observe_project_lock_at(
            ScriptProject,
            'save',
            lambda: self.client.patch(
                self.url(self.project),
                {'description': 'edited'},
                format='json',
                HTTP_IF_MATCH=self.etag(self.project),
                **self.header,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(held, 'The project lock was not held before the row lock.')

    def test_a_conditional_script_file_update_holds_the_project_lock_first(self):
        held, response = self.observe_project_lock_at(
            ScriptFile,
            'save',
            lambda: self.client.patch(
                self.url(self.script_file),
                {'description': 'edited'},
                format='json',
                HTTP_IF_MATCH=self.etag(self.script_file),
                **self.header,
            ),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(held, 'The project lock was not held before the row lock.')

    def test_a_conditional_project_delete_holds_the_project_lock_first(self):
        # Observed on delete() rather than in a pre_delete receiver: the declaration cascade takes
        # the write lock inside the collector, so a receiver would see it held either way.
        held, response = self.observe_project_lock_at(
            ScriptProject,
            'delete',
            lambda: self.client.delete(self.url(self.project), HTTP_IF_MATCH=self.etag(self.project), **self.header),
        )

        self.assertEqual(response.status_code, 204)
        self.assertTrue(held, 'The project lock was not held before the row lock.')

    def test_a_stale_etag_is_still_refused(self):
        response = self.client.patch(
            self.url(self.project),
            {'description': 'edited'},
            format='json',
            HTTP_IF_MATCH='"not-the-current-etag"',
            **self.header,
        )

        self.assertEqual(response.status_code, 412)
        self.project.refresh_from_db()
        self.assertEqual(self.project.description, '')

    def test_an_update_without_the_header_still_takes_the_lock_and_succeeds(self):
        # The unconditional path takes no row lock, so this pins the lock being taken anyway.
        held, response = self.observe_project_lock_at(
            ScriptProject,
            'save',
            lambda: self.client.patch(self.url(self.project), {'description': 'edited'}, format='json', **self.header),
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(held, 'The project lock was not held during an unconditional write.')
