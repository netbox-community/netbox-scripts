from django.urls import reverse

from netbox_scripts.choices import MigrationStateChoices
from netbox_scripts.models import MigrationRun, ScriptProject
from netbox_scripts.tests.plugin_testing import ObjectPermissionTestMixin
from utilities.testing import TestCase, create_test_user


class MigrationRunViewTestCase(ObjectPermissionTestMixin, TestCase):
    """The run detail page: what it shows, and the one permission that reaches it."""

    def setUp(self):
        self.user = create_test_user()
        self.client.force_login(self.user)
        self.migration = MigrationRun.objects.create(
            state=MigrationStateChoices.CUTOVER,
            netbox_version='4.6.8',
            plugin_version='0.0.1',
        )

    def grant(self, *actions):
        return super().grant(ScriptProject, *actions)

    def url(self):
        return reverse('plugins:netbox_scripts:migrationrun', args=[self.migration.pk])

    def test_the_page_needs_the_same_permission_as_the_migration_page(self):
        # One permission covers the whole migration surface, so the page never links a viewer
        # somewhere they cannot follow.
        self.assertHttpStatus(self.client.get(self.url()), 403)
        self.grant('add')
        self.assertHttpStatus(self.client.get(self.url()), 200)

    def test_the_page_shows_the_state_and_the_versions(self):
        self.grant('add')

        body = self.client.get(self.url()).content.decode()

        self.assertIn('Cutover', body)
        self.assertIn('4.6.8', body)
        self.assertIn('0.0.1', body)

    def test_the_page_lists_every_completed_step_oldest_first(self):
        self.grant('add')
        self.migration.record_step('cutover')
        self.migration.record_step('activate')

        body = self.client.get(self.url()).content.decode()

        self.assertIn('Completed steps', body)
        self.assertLess(body.index('cutover'), body.index('activate'))

    def test_the_page_omits_the_step_table_before_any_step_runs(self):
        self.grant('add')

        body = self.client.get(self.url()).content.decode()

        self.assertNotIn('Completed steps', body)

    def test_the_migration_page_links_the_open_run(self):
        self.grant('add')

        body = self.client.get(reverse('plugins:netbox_scripts:migration')).content.decode()

        self.assertIn(self.url(), body)
        self.assertIn('Cutover', body)

    def test_the_migration_page_links_the_most_recent_closed_run(self):
        self.grant('add')
        self.migration.advance(MigrationStateChoices.MIGRATED)
        latest = MigrationRun.objects.create(state=MigrationStateChoices.CUTOVER)
        latest.advance(MigrationStateChoices.MIGRATED)

        body = self.client.get(reverse('plugins:netbox_scripts:migration')).content.decode()

        self.assertInHTML(
            f'<a href="{latest.get_absolute_url()}"><span class="badge text-bg-green">Migrated</span></a>', body
        )
        self.assertNotIn('Not started', body)

    def test_the_migration_page_prefers_an_open_run_to_a_closed_one(self):
        self.grant('add')
        self.migration.advance(MigrationStateChoices.MIGRATED)
        newer = MigrationRun.objects.create(state=MigrationStateChoices.STAGING)

        body = self.client.get(reverse('plugins:netbox_scripts:migration')).content.decode()

        self.assertInHTML(
            f'<a href="{newer.get_absolute_url()}"><span class="badge text-bg-cyan">Staging</span></a>', body
        )

    def test_the_migration_page_says_so_when_no_migration_has_started(self):
        self.grant('add')
        self.migration.delete()

        body = self.client.get(reverse('plugins:netbox_scripts:migration')).content.decode()

        self.assertIn('Not started', body)

    def test_the_recorded_warnings_are_rendered(self):
        # The one place outstanding work is gathered, where only the per-pass Job log showed it.
        self.grant('add')
        self.migration.warnings = ['Permission "built-in scripts" was left withdrawn.']
        self.migration.save(update_fields=('warnings',))

        body = self.client.get(self.migration.get_absolute_url()).content.decode()

        self.assertIn('built-in scripts', body)
        self.assertIn('Outstanding after this migration', body)
