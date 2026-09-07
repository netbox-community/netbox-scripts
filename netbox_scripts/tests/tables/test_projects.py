from django.test import TestCase

from netbox_scripts.models import ScriptProject, ScriptProjectRevision
from netbox_scripts.tables import ScriptProjectRevisionTable, ScriptProjectTable
from utilities.testing import TableTestCases


class ScriptProjectTableTestCase(TableTestCases.StandardTableTestCase):
    table = ScriptProjectTable


class ScriptProjectRevisionTableTestCase(TableTestCases.StandardTableTestCase):
    """
    The revision history table, which renders inside the project detail view.

    The source is declared rather than discovered, because discovery looks for a list view
    declaring the table and revisions deliberately have none. They are history rendered on
    their project, not a CRUD surface of their own.
    """

    table = ScriptProjectRevisionTable
    queryset_sources = (('ScriptProjectView', ScriptProjectRevision.objects.all()),)


class RevisionScriptFileColumnTestCase(TestCase):
    """Two revisions sharing a source digest are told apart by the entrypoint set they froze."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Column Project', key='column-project')
        # Identity is project plus source digest plus entrypoint digest, so one digest with two
        # entrypoint sets is a legal pair and the pair a reader cannot otherwise tell apart.
        cls.one = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            script_file_digest='b' * 64,
            script_file_snapshot=[{'script_file': 1, 'source_path': 'deploy.py'}],
        )
        cls.two = ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            script_file_digest='c' * 64,
            script_file_snapshot=[
                {'script_file': 1, 'source_path': 'audit.py'},
                {'script_file': 2, 'source_path': 'deploy.py'},
            ],
        )

    def count_for(self, revision):
        # One row per table: Meta.order_by is '-created', and a table cannot be ordered by a
        # column it does not declare, so row order is not a thing to assert against here.
        table = ScriptProjectRevisionTable(ScriptProjectRevision.objects.filter(pk=revision.pk))
        return table.rows[0].get_cell('script_file_count')

    def test_the_column_is_offered_by_default(self):
        self.assertIn('script_file_count', ScriptProjectRevisionTable.Meta.default_columns)

    def test_two_revisions_on_one_digest_render_distinguishably(self):
        self.assertEqual(self.one.short_digest, self.two.short_digest)
        self.assertNotEqual(self.count_for(self.one), self.count_for(self.two))
        self.assertEqual([self.count_for(self.one), self.count_for(self.two)], [1, 2])

    def test_a_revision_declaring_no_script_files_renders_a_zero(self):
        empty = ScriptProjectRevision.objects.create(project=self.project, digest='d' * 64, script_file_snapshot=[])

        self.assertEqual(self.count_for(empty), 0)
