from django.urls import reverse

from core.models import ObjectType
from netbox_custom_scripts.choices import MigrationStateChoices
from netbox_custom_scripts.models import CustomScriptProject, MigrationRun
from users.models import ObjectPermission
from utilities.testing import TestCase, create_test_user


class MigrationRunViewTestCase(TestCase):
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
        obj_perm = ObjectPermission(name=f'project {"/".join(actions)}', actions=list(actions))
        obj_perm.save()
        obj_perm.users.add(self.user)
        obj_perm.object_types.add(ObjectType.objects.get_for_model(CustomScriptProject))

    def url(self):
        return reverse('plugins:netbox_custom_scripts:migrationrun', args=[self.migration.pk])

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

        body = self.client.get(reverse('plugins:netbox_custom_scripts:migration')).content.decode()

        self.assertIn(self.url(), body)
        self.assertIn('Cutover', body)

    def test_the_migration_page_says_so_when_no_migration_has_started(self):
        self.grant('add')
        self.migration.delete()

        body = self.client.get(reverse('plugins:netbox_custom_scripts:migration')).content.decode()

        self.assertIn('Not started', body)
