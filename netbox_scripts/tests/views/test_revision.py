from django.test import override_settings
from django.urls import reverse

from core.models import ObjectType
from netbox_scripts import activation
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from netbox_scripts.storage import service
from netbox_scripts.storage.exceptions import ActivationError
from netbox_scripts.tables import (
    CustomScriptProjectRevisionEntrypointTable,
    CustomScriptProjectRevisionProblemTable,
)
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user

REVISION_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
}


def record(class_name='Deploy', position=0):
    """One snapshot record, the shape runtime introspection produces."""
    return {
        'module_path': 'deploy',
        'class_name': class_name,
        'entrypoint_module_id': 1,
        'entrypoint_path': 'deploy.py',
        # Validation refuses a snapshot whose positions are not sequential.
        'position': position,
        'display_name': class_name,
        'description': '',
        'metadata': {},
    }


@override_settings(STORAGES=REVISION_STORAGES)
class RevisionServiceViewTestCase(TestCase):
    """The per-row Activate and Deactivate buttons on a project's Revisions tab."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = CustomScriptProject.objects.create(name='Serviced', key='serviced')

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    counter = 0

    def valid_revision(self, records=None):
        """Stage distinct content, mark it valid, and record what it publishes."""
        type(self).counter += 1
        revision = service.stage_revision(self.project, {'deploy.py': f'V = {self.counter}\n'.encode()}).revision
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALID,
            discovered_scripts=[record()] if records is None else records,
        )
        revision.refresh_from_db()
        return revision

    @staticmethod
    def url(revision, action):
        return reverse(f'plugins:netbox_scripts:customscriptprojectrevision_{action}', args=[revision.pk])

    def tab_url(self):
        return f'{self.project.get_absolute_url()}revisions/'

    def test_activating_puts_the_revision_into_service(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        response = self.client.post(self.url(revision, 'activate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(self.project.active_revision_id, revision.pk)
        self.assertTrue(CustomScript.objects.get(project=self.project).is_executable)

    def test_the_success_message_names_what_the_revision_published(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision(records=[record(), record(class_name='AuditDevices', position=1)])
        response = self.client.post(self.url(revision, 'activate'), follow=True)
        message = str(list(response.context['messages'])[0])

        self.assertIn('publishing 2 Custom Scripts', message)

    def test_the_success_message_is_singular_for_one_script(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        response = self.client.post(self.url(self.valid_revision(), 'activate'), follow=True)
        message = str(list(response.context['messages'])[0])

        self.assertIn('publishing 1 Custom Script.', message)

    def test_a_revision_publishing_nothing_says_so_rather_than_reporting_zero(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        response = self.client.post(self.url(self.valid_revision(records=[]), 'activate'), follow=True)
        message = str(list(response.context['messages'])[0])

        self.assertIn('publishes no Custom Scripts', message)
        self.assertNotIn('publishing 0', message)

    def test_a_retirement_is_reported_separately_from_what_is_published(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        first = self.valid_revision(records=[record(), record(class_name='AuditDevices', position=1)])
        # follow, so the first activation's message is rendered rather than left queued for this one.
        self.client.post(self.url(first, 'activate'), follow=True)
        second = self.valid_revision(records=[record()])
        response = self.client.post(self.url(second, 'activate'), follow=True)
        message = str(list(response.context['messages'])[0])

        self.assertIn('publishing 1 Custom Script.', message)
        self.assertIn('retired 1 Custom Script', message)
        self.assertNotIn('publishing 2', message)

    def test_the_route_refuses_the_revision_already_in_force(self):
        # A stale confirmation page is the realistic way to reach this.
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))

        self.assertHttpStatus(self.client.get(self.url(revision, 'activate')), 404)
        self.assertHttpStatus(self.client.post(self.url(revision, 'activate')), 404)

    def test_the_route_refuses_a_revision_that_never_validated(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        materialized = self.valid_revision()
        CustomScriptProjectRevision.objects.filter(pk=materialized.pk).update(status=RevisionStatusChoices.MATERIALIZED)

        self.assertHttpStatus(self.client.post(self.url(materialized, 'activate')), 404)

    def test_deactivating_retires_the_revision_and_its_scripts(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        response = self.client.post(self.url(revision, 'deactivate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.project.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.RETIRED)
        self.assertIsNone(self.project.active_revision_id)
        self.assertTrue(CustomScript.objects.get(project=self.project).is_retired)

    def test_reactivating_brings_the_same_rows_back(self):
        # The point of retiring rather than deleting: the primary key and the administrator's
        # enabled both survive a round trip through deactivation.
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        script = CustomScript.objects.get(project=self.project)
        CustomScript.objects.filter(pk=script.pk).update(enabled=False)

        self.client.post(self.url(revision, 'deactivate'))
        self.client.post(self.url(revision, 'activate'))

        returned = CustomScript.objects.get(project=self.project)
        self.assertEqual(returned.pk, script.pk)
        self.assertFalse(returned.is_retired)
        self.assertFalse(returned.enabled)

    def test_deactivating_a_revision_that_is_not_active_is_refused(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        response = self.client.post(self.url(revision, 'deactivate'))
        self.assertHttpStatus(response, 302)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_activating_a_second_revision_retires_the_first(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        first = self.valid_revision()
        self.client.post(self.url(first, 'activate'))
        second = self.valid_revision(records=[record(class_name='Later')])
        self.client.post(self.url(second, 'activate'))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, RevisionStatusChoices.RETIRED)
        self.assertEqual(second.status, RevisionStatusChoices.ACTIVE)

    def test_a_get_confirms_without_changing_anything(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        response = self.client.get(self.url(revision, 'activate'))
        self.assertHttpStatus(response, 200)
        self.assertIn(revision.short_digest, response.content.decode())
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_the_deactivate_confirmation_renders(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        response = self.client.get(self.url(revision, 'deactivate'))
        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertIn('Deactivate revision', body)
        self.assertIn(revision.short_digest, body)
        # The confirmation warns that retirement is not deletion, which is the whole contract.
        self.assertIn('retired, not deleted', body)

    def test_the_row_buttons_are_links_and_not_nested_forms(self):
        # The children view wraps its table in a form for bulk actions, and a nested form is
        # invalid HTML that browsers discard, so a button inside one submits the OUTER form to
        # the tab URL. That is exactly what happened: POST to the tab, 405. Links cannot.
        self.grant(CustomScriptProject, 'view', 'activate')
        self.grant(CustomScriptProjectRevision, 'view')
        revision = self.valid_revision()
        body = self.client.get(self.tab_url()).content.decode()
        target = self.url(revision, 'activate')
        self.assertIn(f'href="{target}"', body)
        self.assertNotIn(f'action="{target}"', body)

    def test_the_tab_url_refuses_a_post(self):
        # The 405 the owner hit. Nothing should ever post here, and this pins that the tab is
        # not a state-changing route if a future template regresses to a nested form.
        self.grant(CustomScriptProject, 'view', 'activate')
        self.valid_revision()
        self.assertHttpStatus(self.client.post(self.tab_url()), 405)

    def test_the_project_view_permission_alone_is_not_enough(self):
        self.grant(CustomScriptProject, 'view')
        revision = self.valid_revision()
        self.assertHttpStatus(self.client.post(self.url(revision, 'activate')), 403)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

    def test_no_revision_permission_of_its_own_is_needed(self):
        # The operation changes what the project serves, so the project's permission is the
        # gate. Viewing a revision is separate and takes its own permission, as REST does.
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = self.valid_revision()
        self.assertHttpStatus(self.client.post(self.url(revision, 'activate')), 302)
        revision.refresh_from_db()
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)

    def test_the_tab_offers_activate_for_a_valid_revision_and_nothing_else(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        self.grant(CustomScriptProjectRevision, 'view')
        revision = self.valid_revision()
        body = self.client.get(self.tab_url()).content.decode()
        self.assertIn(self.url(revision, 'activate'), body)
        self.assertNotIn(self.url(revision, 'deactivate'), body)

    def test_the_tab_offers_deactivate_for_the_active_revision(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        self.grant(CustomScriptProjectRevision, 'view')
        revision = self.valid_revision()
        self.client.post(self.url(revision, 'activate'))
        body = self.client.get(self.tab_url()).content.decode()
        self.assertIn(self.url(revision, 'deactivate'), body)
        self.assertNotIn(self.url(revision, 'activate'), body)

    def test_the_tab_offers_neither_button_without_the_activate_permission(self):
        self.grant(CustomScriptProject, 'view')
        revision = self.valid_revision()
        body = self.client.get(self.tab_url()).content.decode()
        self.assertNotIn(self.url(revision, 'activate'), body)

    def test_a_materialized_revision_offers_neither_button(self):
        self.grant(CustomScriptProject, 'view', 'activate')
        revision = service.stage_revision(self.project, {'deploy.py': b'V = 99\n'}).revision
        self.assertFalse(revision.is_activatable)
        body = self.client.get(self.tab_url()).content.decode()
        self.assertNotIn(self.url(revision, 'activate'), body)
        self.assertNotIn(self.url(revision, 'deactivate'), body)


@override_settings(STORAGES=REVISION_STORAGES)
class DeactivateRevisionTestCase(TestCase):
    """The domain operation behind the button."""

    def setUp(self):
        self.project = CustomScriptProject.objects.create(name='Domain', key='domain')

    def activated(self):
        revision = service.stage_revision(self.project, {'deploy.py': b'V = 1\n'}).revision
        CustomScriptProjectRevision.objects.filter(pk=revision.pk).update(
            status=RevisionStatusChoices.VALID, discovered_scripts=[record()]
        )
        revision.refresh_from_db()
        activation.activate_revision(revision)
        revision.refresh_from_db()
        return revision

    def test_it_returns_the_retired_revision(self):
        revision = self.activated()
        self.assertEqual(activation.deactivate_revision(revision).status, RevisionStatusChoices.RETIRED)

    def test_it_refuses_a_revision_that_is_not_active(self):
        revision = self.activated()
        activation.deactivate_revision(revision)
        with self.assertRaises(ActivationError):
            activation.deactivate_revision(revision)

    def test_it_leaves_the_revision_activatable_again(self):
        revision = self.activated()
        activation.deactivate_revision(revision)
        revision.refresh_from_db()
        self.assertTrue(revision.is_activatable)
        self.assertFalse(revision.is_active)


class CustomScriptProjectRevisionProblemPanelTestCase(TestCase):
    """The revision detail view reports the problems its record carries, whichever tier wrote them."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Problem Project', key='problem-project')
        cls.invalid = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            status=RevisionStatusChoices.INVALID,
            validation_errors=[
                {
                    'source_path': 'broken.py',
                    'code': 'invalid_job_timeout',
                    'message': 'The job timeout of "Broken" is not a number of seconds.',
                    'exception_type': None,
                    'traceback': 'Traceback (most recent call last):\n  ValueError',
                }
            ],
        )
        # No digest, the shape the storage tier persists when a manifest rejects a file.
        cls.rejected = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest=None,
            status=RevisionStatusChoices.INVALID,
            validation_errors=[
                {'path': 'notes.txt', 'code': 'not_a_python_file', 'message': 'Only Python files are accepted.'},
                {'path': None, 'code': 'too_many_files', 'message': 'The project has 900 files.'},
            ],
        )
        cls.clean = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='b' * 64,
            status=RevisionStatusChoices.VALID,
        )

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def grant(self, model, *actions):
        obj_perm = ObjectPermission(name=f'{model._meta.model_name} {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(model))

    def body(self, revision):
        url = reverse('plugins:netbox_scripts:customscriptprojectrevision', args=[revision.pk])
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        return response.content.decode()

    def test_a_validation_record_reports_its_path_code_and_message(self):
        self.grant(CustomScriptProjectRevision, 'view')
        body = self.body(self.invalid)
        self.assertIn('Recorded problems', body)
        self.assertIn('broken.py', body)
        self.assertIn('invalid_job_timeout', body)
        self.assertIn('is not a number of seconds', body)

    def test_a_storage_record_reports_its_path_under_the_same_column(self):
        self.grant(CustomScriptProjectRevision, 'view')
        body = self.body(self.rejected)
        self.assertIn('notes.txt', body)
        self.assertIn('not_a_python_file', body)

    def test_a_record_naming_no_file_is_marked_project_wide(self):
        self.grant(CustomScriptProjectRevision, 'view')
        self.assertIn('The whole project', self.body(self.rejected))

    def test_the_traceback_is_available_but_not_shown_by_default(self):
        self.grant(CustomScriptProjectRevision, 'view')
        # Behaviour first, then that the column exists at all, so deleting it fails this too.
        self.assertNotIn('most recent call last', self.body(self.invalid))
        self.assertIn('traceback', CustomScriptProjectRevisionProblemTable.Meta.fields)

    def test_a_revision_with_no_problems_renders_no_panel(self):
        self.grant(CustomScriptProjectRevision, 'view')
        self.assertNotIn('Recorded problems', self.body(self.clean))

    def test_both_shapes_normalize_to_one_row_shape(self):
        rows = self.rejected.problems
        self.assertEqual([row['path'] for row in rows], ['notes.txt', ''])
        self.assertEqual([row['traceback'] for row in rows], ['', ''])
        self.assertEqual(self.invalid.problems[0]['path'], 'broken.py')


class RevisionEntrypointPanelTestCase(TestCase):
    """A revision's page lists the entrypoints it froze, which is where the tab's count resolves."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Entrypoint Project', key='entrypoint-project')
        cls.declared = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            entrypoint_digest='b' * 64,
            entrypoint_snapshot=[
                {'module': 1, 'source_path': 'audit.py'},
                {'module': 2, 'source_path': 'tools/deploy.py'},
            ],
        )
        # Same source tree, different selection: the pair the Revisions tab cannot otherwise
        # tell apart.
        cls.narrowed = CustomScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            entrypoint_digest='c' * 64,
            entrypoint_snapshot=[{'module': 1, 'source_path': 'audit.py'}],
        )
        cls.empty = CustomScriptProjectRevision.objects.create(
            project=cls.project, digest='d' * 64, entrypoint_snapshot=[]
        )

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        obj_perm = ObjectPermission(name='revision view', actions=['view'])
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CustomScriptProjectRevision))

    def body(self, revision):
        url = reverse('plugins:netbox_scripts:customscriptprojectrevision', args=[revision.pk])
        response = self.client.get(url)
        self.assertHttpStatus(response, 200)
        return response.content.decode()

    def test_the_panel_lists_every_frozen_path(self):
        body = self.body(self.declared)

        self.assertIn('Entrypoints', body)
        self.assertIn('audit.py', body)
        self.assertIn('tools/deploy.py', body)

    def test_two_revisions_on_one_digest_list_different_paths(self):
        self.assertEqual(self.declared.short_digest, self.narrowed.short_digest)
        self.assertIn('tools/deploy.py', self.body(self.declared))
        self.assertNotIn('tools/deploy.py', self.body(self.narrowed))

    def test_a_revision_freezing_none_says_so_rather_than_rendering_no_panel(self):
        body = self.body(self.empty)

        self.assertIn('Entrypoints', body)
        self.assertIn('publishes nothing', body)

    def test_the_panel_offers_no_column_but_the_path(self):
        # The snapshot also carries Module primary keys, which are provenance rather than
        # something an operator reads, and the row they name may since have been deleted.
        self.assertEqual(CustomScriptProjectRevisionEntrypointTable.Meta.fields, ('source_path',))
