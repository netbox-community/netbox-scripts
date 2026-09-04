from django.test import TestCase
from django.utils import timezone

from core.models import DataFile, DataSource
from netbox_scripts.choices import ProjectSourceTypeChoices, RevisionStatusChoices
from netbox_scripts.models import CustomScriptProject, ScriptProjectRevision

DIGEST = 'd' * 64


def manifest(*paths):
    return [{'path': path, 'size': 1, 'sha256': DIGEST} for path in sorted(paths)]


class DataSourceCandidatesTestCase(TestCase):
    """A data source is readable before any revision exists, so it is where candidates come from."""

    @classmethod
    def setUpTestData(cls):
        cls.source = DataSource.objects.create(
            name='Scripts Repo',
            type='local',
            source_url='file:///tmp/scripts-repo/',
        )
        for path in (
            'automation/netbox/deploy.py',
            'automation/netbox/tools/audit.py',
            'automation/netbox/bundle/__init__.py',
            'automation/netbox/bundle/helpers.py',
            'automation/netbox/lib/shared.py',
            'automation/netbox/README.md',
            'automation/netbox/config.yaml',
            'automation/netbox-old/legacy.py',
            'unrelated/other.py',
        ):
            # last_updated is editable=False with no auto_now, so a fixture must set it.
            DataFile.objects.create(
                source=cls.source,
                path=path,
                size=1,
                hash=DIGEST,
                data=b'x',
                last_updated=timezone.now(),
            )

        cls.project = CustomScriptProject.objects.create(
            name='Repo Project',
            key='repo-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=cls.source,
            data_path='automation/netbox',
        )

    def test_lists_python_modules_at_every_depth(self):
        self.assertEqual(
            self.project.entrypoint_candidates(),
            [
                'bundle/__init__.py',
                'bundle/helpers.py',
                'deploy.py',
                'lib/shared.py',
                'tools/audit.py',
            ],
        )

    def test_excludes_non_python_files(self):
        candidates = self.project.entrypoint_candidates()
        self.assertNotIn('README.md', candidates)
        self.assertNotIn('config.yaml', candidates)

    def test_the_data_path_prefix_is_matched_by_segment(self):
        # A sibling directory sharing a textual prefix must not leak in.
        self.assertNotIn('legacy.py', self.project.entrypoint_candidates())
        self.assertNotIn('../netbox-old/legacy.py', self.project.entrypoint_candidates())

    def test_excludes_files_outside_the_project_directory(self):
        self.assertNotIn('other.py', self.project.entrypoint_candidates())

    def test_a_project_at_a_sibling_directory_sees_only_its_own(self):
        sibling = CustomScriptProject.objects.create(
            name='Legacy Project',
            key='legacy-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path='automation/netbox-old',
        )
        self.assertEqual(sibling.entrypoint_candidates(), ['legacy.py'])

    def test_an_unsynchronized_data_source_yields_nothing(self):
        empty = DataSource.objects.create(name='Empty', type='local', source_url='file:///tmp/empty/')
        project = CustomScriptProject.objects.create(
            name='Empty Project',
            key='empty-project',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=empty,
            data_path='scripts',
        )
        self.assertEqual(project.entrypoint_candidates(), [])


class RevisionManifestCandidatesTestCase(TestCase):
    """An upload project has no data source, so its newest stored revision is the inventory."""

    @classmethod
    def setUpTestData(cls):
        cls.project = CustomScriptProject.objects.create(name='Upload Project', key='upload-project')

    def test_a_project_without_source_yields_nothing(self):
        self.assertEqual(self.project.entrypoint_candidates(), [])

    def test_reads_the_newest_stored_revision(self):
        ScriptProjectRevision.objects.create(
            project=self.project,
            digest='a' * 64,
            manifest=manifest('old.py'),
            status=RevisionStatusChoices.RETIRED,
        )
        ScriptProjectRevision.objects.create(
            project=self.project,
            digest='b' * 64,
            entrypoint_digest='b' * 64,
            manifest=manifest('deploy.py', 'tools/audit.py', 'notes.txt'),
            status=RevisionStatusChoices.MATERIALIZED,
        )
        self.assertEqual(self.project.entrypoint_candidates(), ['deploy.py', 'tools/audit.py'])

    def test_the_active_revision_wins(self):
        active = ScriptProjectRevision.objects.create(
            project=self.project,
            digest='c' * 64,
            manifest=manifest('active.py'),
            status=RevisionStatusChoices.ACTIVE,
        )
        self.project.active_revision = active
        self.project.save()
        ScriptProjectRevision.objects.create(
            project=self.project,
            digest='e' * 64,
            entrypoint_digest='e' * 64,
            manifest=manifest('staged.py'),
            status=RevisionStatusChoices.MATERIALIZED,
        )
        self.assertEqual(self.project.entrypoint_candidates(), ['active.py'])

    def test_a_revision_without_a_digest_is_ignored(self):
        ScriptProjectRevision.objects.create(
            project=self.project,
            manifest=manifest('rejected.py'),
            status=RevisionStatusChoices.INVALID,
        )
        self.assertEqual(self.project.entrypoint_candidates(), [])
