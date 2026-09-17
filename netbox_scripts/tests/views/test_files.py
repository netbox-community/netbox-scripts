from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin
from utilities.testing import TestCase, create_test_user


class ScriptProjectFilesViewTestCase(ObjectPermissionTestMixin, TestCase):
    """The Revision Files tab lists the current revision's manifest with the live declaration state."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Files Project', key='files-project')
        cls.revision = ScriptProjectRevision.objects.create(
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
        ScriptFile.objects.create(project=cls.project, source_path='deploy.py', enabled=True)
        ScriptFile.objects.create(project=cls.project, source_path='removed.py', enabled=True)
        cls.empty = ScriptProject.objects.create(name='Empty Project', key='empty-project')

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)

    def url(self, project):
        return reverse('plugins:netbox_scripts:scriptproject_files', args=[project.pk])

    def test_the_tab_lists_the_manifest(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
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
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(self.project)).content.decode()
        self.assertIn('removed.py (missing from the source)', body)

    def test_a_declared_path_only_a_newer_revision_holds_reads_as_not_yet_active(self):
        project, _newer = self.project_serving_an_older_revision()
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (not in the active revision yet)', body)
        self.assertNotIn('added.py (missing from the source)', body)

    def test_a_path_only_an_invalid_revision_holds_reads_as_missing(self):
        # An invalid revision can never be activated, so promising activation would be a worse
        # lie than the wording this replaced.
        project, _newer = self.project_serving_an_older_revision(RevisionStatusChoices.INVALID)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (missing from the source)', body)
        self.assertNotIn('added.py (not in the active revision yet)', body)

    def test_an_invalid_newest_revision_does_not_mask_an_activatable_one_behind_it(self):
        project, _newer = self.project_serving_an_older_revision()
        ScriptProjectRevision.objects.create(
            project=project,
            digest='2' * 64,
            status=RevisionStatusChoices.INVALID,
            manifest=[{'path': 'deploy.py', 'size': 10, 'sha256': 'e' * 64}],
            file_count=1,
            total_size=10,
        )
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (not in the active revision yet)', body)

    def test_the_two_absences_are_distinguished_on_one_project(self):
        project, _newer = self.project_serving_an_older_revision()
        ScriptFile.objects.create(project=project, source_path='gone.py', enabled=True)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (not in the active revision yet)', body)
        self.assertIn('gone.py (missing from the source)', body)

    @staticmethod
    def project_serving_an_older_revision(newer_status=RevisionStatusChoices.VALID):
        """Return a project whose active revision is older than its newest stored one."""
        project = ScriptProject.objects.create(name='Staged Project', key='staged-project')
        active = ScriptProjectRevision.objects.create(
            project=project,
            digest='d' * 64,
            status=RevisionStatusChoices.ACTIVE,
            manifest=[{'path': 'deploy.py', 'size': 10, 'sha256': 'e' * 64}],
            file_count=1,
            total_size=10,
        )
        newer = ScriptProjectRevision.objects.create(
            project=project,
            digest='f' * 64,
            status=newer_status,
            manifest=[
                {'path': 'deploy.py', 'size': 10, 'sha256': 'e' * 64},
                {'path': 'added.py', 'size': 20, 'sha256': '1' * 64},
            ],
            file_count=2,
            total_size=30,
        )
        # created is auto_now_add, so the ordering latest_stored_revision() reads is set here
        # rather than left to two writes landing in the same microsecond.
        ScriptProjectRevision.objects.filter(pk=active.pk).update(created=timezone.now() - timedelta(hours=2))
        ScriptProjectRevision.objects.filter(pk=newer.pk).update(created=timezone.now() - timedelta(hours=1))
        ScriptProject.objects.filter(pk=project.pk).update(active_revision=active)
        ScriptFile.objects.create(project=project, source_path='added.py', enabled=True)
        return ScriptProject.objects.get(pk=project.pk), newer

    def test_the_script_file_column_reads_the_live_declaration(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        table = self.client.get(self.url(self.project)).context['table']
        state = {row.record['path']: row.record['script_file'] for row in table.rows}
        self.assertTrue(state['deploy.py'])
        self.assertFalse(state['helpers.py'])
        self.assertTrue(state['removed.py'])

    def test_the_tab_is_empty_without_permission_on_its_revision(self):
        self.grant(ScriptProject, 'view')

        response = self.client.get(self.url(self.project))

        self.assertHttpStatus(response, 200)
        body = response.content.decode()
        self.assertNotIn('deploy.py', body)
        self.assertNotIn('a' * 64, body)

    def test_a_project_without_content_shows_the_empty_state(self):
        self.grant(ScriptProject, 'view')
        response = self.client.get(self.url(self.empty))
        self.assertHttpStatus(response, 200)
        self.assertIn('This project has no stored revision yet.', response.content.decode())

    def test_the_view_permission_is_required(self):
        self.assertHttpStatus(self.client.get(self.url(self.project)), 403)

    def test_the_tab_is_linked_from_the_detail_page(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.project.get_absolute_url()).content.decode()
        self.assertIn(self.url(self.project), body)

    def test_the_tab_names_the_revision_it_lists(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(self.project)).content.decode()
        self.assertIn('current revision', body)
        # The revision's own string form, which is the short digest, so the line names which one.
        self.assertIn('a' * 12, body)
        self.assertNotIn('a' * 64, body)
        self.assertIn(self.revision.get_absolute_url(), body)
        self.assertIn(reverse('plugins:netbox_scripts:scriptproject_script_files', args=[self.project.pk]), body)

    def test_the_line_is_absent_from_a_project_holding_no_revision(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(self.empty)).content.decode()
        self.assertNotIn('current revision', body)
