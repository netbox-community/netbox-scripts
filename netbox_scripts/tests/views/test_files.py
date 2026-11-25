from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape

from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.constants import MAX_SOURCE_DISPLAY_SIZE
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision
from netbox_scripts.storage import config, service
from netbox_scripts.storage.paths import revision_key
from netbox_scripts.tests.plugin_testing import IN_MEMORY_STORAGES, ObjectPermissionTestMixin
from netbox_scripts.ui import ScriptFileDiscoveryPanel, ScriptFilePanel, ScriptProjectRevisionFilePanel
from utilities.testing import TestCase, create_test_user


def file_url(revision, file_index):
    """Return the File tab of one file of a revision, addressed by its manifest position."""
    return reverse(
        'plugins:netbox_scripts:scriptprojectrevision_file', kwargs={'pk': revision.pk, 'file_index': file_index}
    )


def source_url(revision, file_index):
    """Return the Source tab of one file of a revision."""
    return reverse(
        'plugins:netbox_scripts:scriptprojectrevision_source', kwargs={'pk': revision.pk, 'file_index': file_index}
    )


def rejected_revision(project):
    """Create the row a rejected upload leaves, without a digest and listing the files it accepted."""
    return ScriptProjectRevision.objects.create(
        project=project,
        digest=None,
        status=RevisionStatusChoices.INVALID,
        manifest=[{'path': 'deploy.py', 'size': 6, 'sha256': 'b' * 64}],
        file_count=1,
        total_size=6,
    )


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

    def test_a_deselected_path_only_a_superseded_revision_holds_reads_as_missing(self):
        project, _newer = self.project_serving_an_older_revision(RevisionStatusChoices.RETIRED)
        ScriptProject.objects.filter(pk=project.pk).update(source_revision_id=project.active_revision_id)
        ScriptFile.objects.filter(project=project, source_path='added.py').update(enabled=False)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (missing from the source)', body)
        self.assertNotIn('added.py (not in the active revision yet)', body)
        project.refresh_from_db()
        with self.assertNumQueries(0):
            self.assertEqual(project.paths_awaiting_activation(), set())

    def test_a_path_an_older_accepted_revision_holds_reads_as_not_yet_active(self):
        project = ScriptProject.objects.create(name='Reverted Project', key='reverted-project')
        accepted = ScriptProjectRevision.objects.create(
            project=project,
            digest='d' * 64,
            status=RevisionStatusChoices.RETIRED,
            manifest=[
                {'path': 'deploy.py', 'size': 10, 'sha256': 'e' * 64},
                {'path': 'added.py', 'size': 20, 'sha256': '1' * 64},
            ],
            file_count=2,
            total_size=30,
        )
        active = ScriptProjectRevision.objects.create(
            project=project,
            digest='f' * 64,
            status=RevisionStatusChoices.ACTIVE,
            manifest=[{'path': 'deploy.py', 'size': 10, 'sha256': 'e' * 64}],
            file_count=1,
            total_size=10,
        )
        ScriptProjectRevision.objects.filter(pk=accepted.pk).update(created=timezone.now() - timedelta(hours=2))
        ScriptProject.objects.filter(pk=project.pk).update(active_revision=active, source_revision=accepted)
        ScriptFile.objects.create(project=project, source_path='added.py', enabled=False)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (not in the active revision yet)', body)
        self.assertNotIn('added.py (missing from the source)', body)

    def test_a_path_a_pending_accepted_source_holds_reads_as_not_yet_active(self):
        project, newer = self.project_serving_an_older_revision(RevisionStatusChoices.MATERIALIZED)
        ScriptProject.objects.filter(pk=project.pk).update(source_revision=newer)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (not in the active revision yet)', body)
        self.assertNotIn('added.py (missing from the source)', body)

    def test_a_superseded_valid_revision_does_not_stand_in_for_an_invalid_accepted_source(self):
        project, _newer = self.project_serving_an_older_revision()
        invalid = ScriptProjectRevision.objects.create(
            project=project,
            digest='2' * 64,
            status=RevisionStatusChoices.INVALID,
            manifest=[
                {'path': 'deploy.py', 'size': 12, 'sha256': '3' * 64},
                {'path': 'added.py', 'size': 20, 'sha256': '1' * 64},
            ],
            file_count=2,
            total_size=32,
        )
        ScriptProject.objects.filter(pk=project.pk).update(source_revision=invalid)
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        body = self.client.get(self.url(project)).content.decode()
        self.assertIn('added.py (missing from the source)', body)
        self.assertNotIn('added.py (not in the active revision yet)', body)

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

    def test_stored_files_link_to_their_source_and_missing_paths_do_not(self):
        self.grant(ScriptProject, 'view')
        self.grant(ScriptProjectRevision, 'view')
        # Sorts first, so a row's position is not its manifest index.
        ScriptFile.objects.create(project=self.project, source_path='aaa.py', enabled=True)
        table = self.client.get(self.url(self.project)).context['table']

        links = {row.record['path']: table.columns['path'].link.compose_url(record=row.record) for row in table.rows}

        self.assertEqual(
            links,
            {
                'aaa.py': None,
                'deploy.py': file_url(self.revision, 0),
                'helpers.py': file_url(self.revision, 1),
                'removed.py': None,
            },
        )


class ScriptProjectRevisionFilesViewTestCase(ObjectPermissionTestMixin, TestCase):
    """A revision's Files tab lists its manifest as the revision froze it, each stored path linked to its source."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='History Project', key='history-project')
        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            status=RevisionStatusChoices.RETIRED,
            manifest=[
                {'path': 'deploy.py', 'size': 120, 'sha256': 'b' * 64},
                {'path': 'helpers.py', 'size': 40, 'sha256': 'c' * 64},
            ],
            file_count=2,
            total_size=160,
            script_file_snapshot=[{'script_file': 1, 'source_path': 'deploy.py'}],
        )
        # The declarations moved on, so only the snapshot says what this revision served.
        ScriptFile.objects.create(project=cls.project, source_path='helpers.py', enabled=True)

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.grant(ScriptProjectRevision, 'view')

    def url(self, revision):
        return reverse('plugins:netbox_scripts:scriptprojectrevision_files', args=[revision.pk])

    def test_the_tab_lists_the_files_as_the_revision_froze_them(self):
        table = self.client.get(self.url(self.revision)).context['table']

        rows = {row.record['path']: row.record for row in table.rows}

        self.assertEqual(list(rows), ['deploy.py', 'helpers.py'])
        self.assertEqual([record['script_file'] for record in rows.values()], [True, False])
        links = {path: table.columns['path'].link.compose_url(record=record) for path, record in rows.items()}
        self.assertEqual(links, {'deploy.py': file_url(self.revision, 0), 'helpers.py': file_url(self.revision, 1)})

    def test_the_tab_is_offered_on_the_revision_page(self):
        body = self.client.get(self.revision.get_absolute_url()).content.decode()

        self.assertIn(self.url(self.revision), body)

    def test_a_revision_holding_no_files_says_so(self):
        empty = ScriptProjectRevision.objects.create(project=self.project, digest='d' * 64)

        self.assertIn('This revision holds no files.', self.client.get(self.url(empty)).content.decode())

    def test_a_revision_rejected_before_storage_lists_its_files_unlinked(self):
        table = self.client.get(self.url(rejected_revision(self.project))).context['table']

        self.assertEqual([row.record['path'] for row in table.rows], ['deploy.py'])
        self.assertIsNone(table.columns['path'].link.compose_url(record=table.rows[0].record))

    def test_sorting_and_paging_keep_each_link_on_its_own_file(self):
        manifest = [{'path': f'module_{n}.py', 'size': 1, 'sha256': 'f' * 64} for n in range(8)]
        revision = ScriptProjectRevision.objects.create(
            project=self.project,
            digest='e' * 64,
            status=RevisionStatusChoices.VALID,
            manifest=manifest,
            file_count=8,
            total_size=8,
        )

        table = self.client.get(self.url(revision), {'sort': '-path', 'per_page': 2, 'page': 2}).context['table']

        paths = [row.record['path'] for row in table.page.object_list]
        links = [table.columns['path'].link.compose_url(record=row.record) for row in table.page.object_list]
        self.assertEqual(table.page.number, 2)
        self.assertEqual(paths, sorted(paths, reverse=True))
        positions = {entry['path']: index for index, entry in enumerate(manifest)}
        self.assertEqual(links, [file_url(revision, positions[path]) for path in paths])

    def test_a_frozen_script_file_the_source_lacks_is_listed_unlinked(self):
        revision = ScriptProjectRevision.objects.create(
            project=self.project,
            digest='g' * 64,
            status=RevisionStatusChoices.VALID,
            manifest=[{'path': 'deploy.py', 'size': 120, 'sha256': 'b' * 64}],
            file_count=1,
            total_size=120,
            script_file_snapshot=[
                {'script_file': 1, 'source_path': 'aaa.py'},
                {'script_file': 2, 'source_path': 'deploy.py'},
            ],
        )

        response = self.client.get(self.url(revision))

        table = response.context['table']
        rows = {row.record['path']: row.record for row in table.rows}
        self.assertEqual(list(rows), ['aaa.py', 'deploy.py'])
        self.assertEqual([record['script_file'] for record in rows.values()], [True, True])
        links = {path: table.columns['path'].link.compose_url(record=record) for path, record in rows.items()}
        self.assertEqual(links, {'aaa.py': None, 'deploy.py': file_url(revision, 0)})
        self.assertIn('aaa.py (missing from the source)', response.content.decode())


class ScriptProjectRevisionFileViewTestCase(ObjectPermissionTestMixin, TestCase):
    """A revision file's File tab shows its manifest entry, Project and Script File, without reading storage."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='File Project', key='file-project')
        cls.revision = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            status=RevisionStatusChoices.VALID,
            manifest=[{'path': 'deploy.py', 'size': 120, 'sha256': 'b' * 64}],
            file_count=1,
            total_size=120,
        )

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.grant(ScriptProjectRevision, 'view')

    def test_the_file_tab_shows_the_entry_without_reading_storage(self):
        storage = config.get_storage()

        with (
            mock.patch.object(storage, 'open', wraps=storage.open) as opened,
            mock.patch.object(storage, 'size', wraps=storage.size) as sized,
        ):
            response = self.client.get(file_url(self.revision, 0))

        self.assertHttpStatus(response, 200)
        opened.assert_not_called()
        sized.assert_not_called()
        self.assertEqual(
            response.context['source_file'],
            {'path': 'deploy.py', 'size': 120, 'sha256': 'b' * 64, 'project': self.project, 'script_file': None},
        )
        self.assertEqual(
            list(ScriptProjectRevisionFilePanel._attrs), ['project', 'path', 'size', 'sha256', 'script_file']
        )
        self.assertTemplateNotUsed(response, 'tabs/model_view_tabs.html')
        self.assertEqual(response.context['file_tab'], 'file')
        self.assertIn(source_url(self.revision, 0), response.content.decode())

    def test_a_declared_path_links_its_script_file_within_the_grant(self):
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py', enabled=True)

        self.assertIsNone(self.client.get(file_url(self.revision, 0)).context['source_file']['script_file'])
        self.grant(ScriptFile, 'view')
        self.assertEqual(self.client.get(file_url(self.revision, 0)).context['source_file']['script_file'], script_file)

    def test_discovery_shows_only_when_it_read_this_revision(self):
        self.grant(ScriptFile, 'view')
        other = ScriptProjectRevision.objects.create(
            project=self.project, digest='c' * 64, status=RevisionStatusChoices.RETIRED
        )
        script_file = ScriptFile.objects.create(
            project=self.project, source_path='deploy.py', enabled=True, last_discovered_revision=self.revision
        )

        self.assertEqual(self.client.get(file_url(self.revision, 0)).context['discovered_file'], script_file)
        ScriptFile.objects.filter(pk=script_file.pk).update(last_discovered_revision=other)
        self.assertIsNone(self.client.get(file_url(self.revision, 0)).context['discovered_file'])

    def test_an_index_outside_the_manifest_is_not_found(self):
        self.assertHttpStatus(self.client.get(file_url(self.revision, 1)), 404)

    def test_related_objects_link_to_their_pages(self):
        file_attrs = ScriptProjectRevisionFilePanel._attrs

        self.assertTrue(file_attrs['project'].linkify)
        self.assertTrue(file_attrs['script_file'].linkify)
        self.assertTrue(ScriptFileDiscoveryPanel._attrs['last_discovered_revision'].linkify)


class ScriptProjectRevisionSourceViewTestCase(ObjectPermissionTestMixin, TestCase):
    """A revision file's Source tab shows its verified text, or why it withholds the text."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = ScriptProject.objects.create(name='Source Project', key='source-project')

    def stored_revision(self, files, status=RevisionStatusChoices.VALID):
        """Stage files as a revision of this Project, moved to status."""
        revision = service.stage_revision(self.project, files).revision
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=status)
        revision.refresh_from_db()
        return revision

    def source(self, revision, file_index=0):
        """GET one file's Source tab."""
        return self.client.get(source_url(revision, file_index))

    def test_the_page_shows_the_stored_text_and_names_its_revision(self):
        self.grant(ScriptProjectRevision, 'view')
        # A form feed and a tab are text, unlike NUL.
        text = '# Déploiement\n\x0c\nV = 1\t# un\n'
        revision = self.stored_revision({'deploy.py': text.encode()})

        response = self.source(revision)

        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context['source_file']['text'], text)
        self.assertEqual(response.context['file_tab'], 'source')
        self.assertTemplateNotUsed(response, 'tabs/model_view_tabs.html')
        body = response.content.decode()
        self.assertIn('Déploiement', body)
        self.assertIn(f'Revision {revision.short_digest} (ID {revision.pk}) of Source Project', body)

    def test_a_file_link_keeps_its_revision_when_another_is_activated(self):
        self.grant(ScriptProjectRevision, 'view')
        first = self.stored_revision({'deploy.py': b'V = 1\n'}, RevisionStatusChoices.ACTIVE)
        link = source_url(first, 0)
        second = self.stored_revision({'deploy.py': b'V = 2\n'})
        ScriptProjectRevision.objects.filter(pk=first.pk).update(status=RevisionStatusChoices.RETIRED)
        ScriptProjectRevision.objects.filter(pk=second.pk).update(status=RevisionStatusChoices.ACTIVE)
        ScriptProject.objects.filter(pk=self.project.pk).update(active_revision=second)

        self.assertEqual(self.client.get(link).context['source_file']['text'], 'V = 1\n')
        self.assertEqual(self.source(second).context['source_file']['text'], 'V = 2\n')

    def test_the_text_is_escaped(self):
        self.grant(ScriptProjectRevision, 'view')
        text = '# <script>alert(1)</script>\n'
        revision = self.stored_revision({'deploy.py': text.encode()})

        body = self.source(revision).content.decode()

        self.assertIn(escape(text), body)
        self.assertNotIn('<script>alert(1)</script>', body)

    def test_the_file_is_read_without_being_imported(self):
        self.grant(ScriptProjectRevision, 'view')
        text = "raise RuntimeError('imported')\n"
        revision = self.stored_revision({'deploy.py': text.encode()})

        response = self.source(revision)

        self.assertHttpStatus(response, 200)
        self.assertEqual(response.context['source_file']['text'], text)

    def test_running_a_script_does_not_grant_its_source(self):
        self.grant(ScriptProject, 'view')
        self.grant(NetBoxScript, 'view', 'run')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})

        self.assertHttpStatus(self.source(revision), 403)

    def test_a_revision_outside_the_grant_is_not_found(self):
        self.grant(ScriptProjectRevision, 'view', constraints={'project__key': 'another-project'})
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})
        storage = config.get_storage()

        with mock.patch.object(storage, 'open', wraps=storage.open) as opened:
            self.assertHttpStatus(self.source(revision), 404)

        opened.assert_not_called()
        self.grant(ScriptProjectRevision, 'view', constraints={'project__key': self.project.key})
        self.assertHttpStatus(self.source(revision), 200)

    def test_an_index_outside_the_manifest_is_not_found(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})
        storage = config.get_storage()

        with mock.patch.object(storage, 'open', wraps=storage.open) as opened:
            self.assertHttpStatus(self.source(revision, 1), 404)

        opened.assert_not_called()

    def test_a_file_over_the_limit_is_named_and_never_opened(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'data.py': b'#' * (MAX_SOURCE_DISPLAY_SIZE + 1)})
        storage = config.get_storage()

        with mock.patch.object(storage, 'open', wraps=storage.open) as opened:
            response = self.source(revision)

        opened.assert_not_called()
        self.assertIsNone(response.context['source_file']['text'])
        self.assertEqual(
            str(response.context['source_file']['unavailable']),
            f'"data.py" is {MAX_SOURCE_DISPLAY_SIZE + 1} bytes, over the {MAX_SOURCE_DISPLAY_SIZE} byte limit for '
            'displaying source.',
        )

    def test_a_file_at_the_limit_is_shown(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'data.py': b'#' * MAX_SOURCE_DISPLAY_SIZE})

        self.assertEqual(len(self.source(revision).context['source_file']['text']), MAX_SOURCE_DISPLAY_SIZE)

    def test_a_binary_file_is_explained(self):
        self.grant(ScriptProjectRevision, 'view')
        # The PNG header fails UTF-8 decoding, and the other two decode but hold NUL.
        revision = self.stored_revision(
            {
                'blob.bin': b'\x00\x01payload',
                'logo.png': b'\x89PNG\r\n\x1a\n',
                'utf16.py': 'print(1)\n'.encode('utf-16-le'),
            }
        )

        self.assertEqual([entry['path'] for entry in revision.manifest], ['blob.bin', 'logo.png', 'utf16.py'])
        for file_index, entry in enumerate(revision.manifest):
            with self.subTest(path=entry['path']):
                response = self.source(revision, file_index)

                self.assertIsNone(response.context['source_file']['text'])
                self.assertEqual(
                    str(response.context['source_file']['unavailable']),
                    'This file is binary or not UTF-8 encoded, so it cannot be displayed.',
                )

    def test_an_empty_file_reads_as_empty(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'tools/__init__.py': b''})

        response = self.source(revision)

        self.assertEqual(response.context['source_file']['text'], '')
        self.assertIsNone(response.context['source_file']['unavailable'])
        self.assertIn('This file is empty.', response.content.decode())

    def test_stored_content_that_no_longer_matches_is_withheld(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})
        storage = config.get_storage()
        key = revision_key(self.project.storage_key, revision.digest, 'deploy.py')
        storage.delete(key)
        # The same length, so only the checksum can tell.
        storage.save(key, ContentFile(b'V = 9\n'))

        response = self.source(revision)

        self.assertEqual(
            str(response.context['source_file']['unavailable']),
            'The stored file "deploy.py" does not match its manifest entry.',
        )
        self.assertNotIn('V = 9', response.content.decode())
        storage.delete(key)
        self.assertEqual(
            str(self.source(revision).context['source_file']['unavailable']),
            'The stored file "deploy.py" is missing.',
        )

    def test_a_manifest_that_cannot_be_trusted_is_never_read(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})
        entry = revision.manifest[0]
        storage = config.get_storage()
        cases = {
            'path': ([{**entry, 'path': '../outside.py'}], 'The stored manifest cannot be trusted.'),
            'entry': ([None], 'The stored manifest cannot be trusted.'),
            'mapping': ({'deploy.py': entry}, 'The stored manifest is not a list.'),
        }

        for case, (manifest, message) in cases.items():
            with self.subTest(case=case), mock.patch.object(storage, 'open', wraps=storage.open) as opened:
                # Only a raw write reaches a frozen manifest.
                ScriptProjectRevision.objects.filter(pk=revision.pk).update(manifest=manifest)

                response = self.source(revision)

                opened.assert_not_called()
                self.assertEqual(response.context['source_file'], {'text': None, 'unavailable': message})

    def test_a_backend_failure_reports_a_fixed_message_and_logs_its_cause(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})
        error = PermissionError(13, 'Permission denied', '/srv/netbox/private/deploy.py')

        with (
            mock.patch.object(config.get_storage(), 'open', side_effect=error),
            self.assertLogs('netbox.plugins.netbox_scripts.views', 'ERROR') as logged,
        ):
            response = self.source(revision)

        self.assertEqual(str(response.context['source_file']['unavailable']), 'The stored file could not be read.')
        self.assertNotIn('/srv/netbox/private', response.content.decode())
        self.assertIs(logged.records[0].exc_info[1].__cause__, error)

    def test_a_revision_rejected_before_storage_has_no_source_page(self):
        self.grant(ScriptProjectRevision, 'view')

        self.assertHttpStatus(self.source(rejected_revision(self.project)), 404)

    def test_files_with_identical_content_open_their_own_pages(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'a/__init__.py': b'', 'b/__init__.py': b''})
        files = reverse('plugins:netbox_scripts:scriptprojectrevision_files', args=[revision.pk])
        table = self.client.get(files).context['table']

        links = {row.record['path']: table.columns['path'].link.compose_url(record=row.record) for row in table.rows}

        self.assertEqual(links, {'a/__init__.py': file_url(revision, 0), 'b/__init__.py': file_url(revision, 1)})
        for path, url in links.items():
            self.assertEqual(self.client.get(url).context['source_file']['path'], path)

    def test_a_byte_order_mark_is_not_shown(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'\xef\xbb\xbfV = 1\n'})

        self.assertEqual(self.source(revision).context['source_file']['text'], 'V = 1\n')

    def test_the_page_is_not_cacheable(self):
        self.grant(ScriptProjectRevision, 'view')
        revision = self.stored_revision({'deploy.py': b'V = 1\n'})

        response = self.source(revision)

        self.assertHttpStatus(response, 200)
        self.assertNotCacheable(response)


class ScriptFileServedLinksTestCase(ObjectPermissionTestMixin, TestCase):
    """A Script File's panel links its file in the revision its Project serves, when the user can open it."""

    def setUp(self):
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.project = ScriptProject.objects.create(name='Served Project', key='served-project')
        self.grant(ScriptFile, 'view')

    def served_revision(self, files):
        """Stage files as this Project's newest stored revision."""
        revision = service.stage_revision(self.project, files).revision
        ScriptProjectRevision.objects.filter(pk=revision.pk).update(status=RevisionStatusChoices.VALID)
        revision.refresh_from_db()
        return revision

    def panel_links(self, script_file):
        """Return the URLs the Script File panel's actions give the test user for a fresh copy of script_file."""
        # Fresh copies, since served_position and the user's permissions are cached on their instances.
        request = RequestFactory().get(script_file.get_absolute_url())
        request.user = get_user_model().objects.get(pk=self.user.pk)
        context = {'object': ScriptFile.objects.get(pk=script_file.pk), 'request': request}
        return [action.get_url(context) for action in ScriptFilePanel.actions]

    def test_the_panel_links_the_served_file_only_when_the_user_can_open_it(self):
        revision = self.served_revision({'deploy.py': b'V = 1\n'})
        served = ScriptFile.objects.create(project=self.project, source_path='deploy.py', enabled=True)
        gone = ScriptFile.objects.create(project=self.project, source_path='gone.py', enabled=True)

        self.assertEqual(self.panel_links(served), [None, None])
        self.grant(ScriptProjectRevision, 'view', constraints={'project__key': 'another-project'})
        self.assertEqual(self.panel_links(served), [None, None])
        self.grant(ScriptProjectRevision, 'view', constraints={'project__key': self.project.key})
        self.assertEqual(self.panel_links(served), [file_url(revision, 0), source_url(revision, 0)])
        self.assertEqual(self.panel_links(gone), [None, None])
        self.assertIn(source_url(revision, 0), self.client.get(served.get_absolute_url()).content.decode())
