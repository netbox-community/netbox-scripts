"""
Integration tests against a real provisioned NetBox Branching branch.

Every test is skipped when NetBox Branching is absent, so the default suite is unchanged. The cases
are TransactionTestCase: a branch lives in its own PostgreSQL schema on its own connection, which no
savepoint rolls back.
"""

import os
import time
import unittest
import uuid

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from core.models import ObjectType
from dcim.models import Site
from extras.models import JournalEntry, Tag
from netbox.context_managers import event_tracking
from netbox_custom_scripts.models import CustomScriptProject

# Branching can be importable while absent from INSTALLED_APPS, and defining one of its models in
# that state raises RuntimeError, which no ImportError guard would catch.
HAS_BRANCHING = apps.is_installed('netbox_branching')

if HAS_BRANCHING:
    from netbox_branching.choices import BranchMergeStrategyChoices, BranchStatusChoices  # noqa: F401
    from netbox_branching.models import Branch, ChangeDiff
    from netbox_branching.provisioning import quote_ident
    from netbox_branching.utilities import activate_branch, get_tables_to_replicate

User = get_user_model()

# Not TestCase subclasses without Branching, so the runner never reaches their machinery at all.
_TestBase = TransactionTestCase if HAS_BRANCHING else object

PROVISION_TIMEOUT = float(os.environ.get('NETBOX_CS_BRANCH_PROVISION_TIMEOUT', '60'))


def provision_branch(name, merge_strategy=None, user=None, timeout=None):
    """
    Return a branch provisioned and waited on until it reports READY.

    Raises TimeoutError naming the status it stopped at.
    """
    branch = Branch(name=name, merge_strategy=merge_strategy)
    branch.save(provision=False)
    branch.provision(user=user)
    deadline = time.time() + (PROVISION_TIMEOUT if timeout is None else timeout)
    # A partial provision reports a status instead of raising, so polling it is the only way to
    # tell a slow branch from a failed one.
    while time.time() < deadline:
        branch.refresh_from_db()
        if branch.status == BranchStatusChoices.READY:
            return branch
        time.sleep(0.1)
    raise TimeoutError(f'Branch {name!r} stopped at status {branch.status!r}')


@unittest.skipUnless(HAS_BRANCHING, 'netbox_branching is not installed')
class BranchingTestCase(_TestBase):
    """Provision branches and drop their schemas afterwards, whatever the test did."""

    # Provisioning reads main-schema rows, which a preceding TransactionTestCase truncated.
    serialized_rollback = True

    def setUp(self):
        super().setUp()
        self._schemas = []
        self.user = User.objects.create_user(username='branchuser')
        self.request = self.make_request(self.user)

    def tearDown(self):
        # Phase 1 of provision() commits its CREATE SCHEMA, so rollback leaves the schema behind
        # and a --keepdb run would accumulate one per test.
        for schema in self._schemas:
            with connection.cursor() as cursor:
                cursor.execute(f'DROP SCHEMA IF EXISTS {quote_ident(schema)} CASCADE')
        super().tearDown()

    def make_request(self, user):
        """Return the request object change logging needs in order to record anything at all."""
        request = RequestFactory().get(reverse('home'))
        request.id = uuid.uuid4()
        request.user = user
        return request

    def branch(self, name, **kwargs):
        """Return a READY branch whose schema this test will drop."""
        branch = provision_branch(name, user=self.user, **kwargs)
        self._schemas.append(branch.schema_name)
        return branch


class ProvisioningTestCase(BranchingTestCase):
    def test_netbox_branching_is_actually_installed(self):
        # An incompatible plugin is skipped with a warning rather than failing, so a green run
        # proves nothing by itself.
        self.assertTrue(apps.is_installed('netbox_branching'))

    def test_a_branch_provisions_with_this_plugin_installed(self):
        branch = self.branch('Smoke')
        self.assertEqual(branch.status, BranchStatusChoices.READY)

    def test_no_table_of_this_plugin_is_replicated_into_the_branch_schema(self):
        # The routing claim at its strongest: the tables do not physically exist in the branch.
        branch = self.branch('Tables')
        with connection.cursor() as cursor:
            cursor.execute(
                'SELECT table_name FROM information_schema.tables WHERE table_schema = %s',
                (branch.schema_name,),
            )
            replicated = {row[0] for row in cursor.fetchall()}
        self.assertTrue(replicated, 'the branch schema holds no tables at all, so it never provisioned')
        self.assertEqual([name for name in replicated if name.startswith('netbox_custom_scripts_')], [])

    def test_the_replication_list_excludes_this_plugin(self):
        self.assertEqual(
            [table for table in get_tables_to_replicate() if table.startswith('netbox_custom_scripts_')],
            [],
        )


class ActiveBranchTestCase(BranchingTestCase):
    def test_a_project_created_inside_a_branch_is_visible_outside_it(self):
        branch = self.branch('Writes')
        with activate_branch(branch):
            project = CustomScriptProject.objects.create(name='Inside', key='inside')
        # No activate_branch here, so the main schema is what answers.
        self.assertTrue(CustomScriptProject.objects.filter(pk=project.pk).exists())

    def test_a_project_created_outside_is_readable_inside_a_branch(self):
        project = CustomScriptProject.objects.create(name='Outside', key='outside')
        branch = self.branch('Reads')
        with activate_branch(branch):
            self.assertTrue(CustomScriptProject.objects.filter(pk=project.pk).exists())

    def test_an_edit_inside_a_branch_applies_globally(self):
        project = CustomScriptProject.objects.create(name='Before', key='edited')
        branch = self.branch('Edits')
        with activate_branch(branch):
            project.name = 'After'
            project.save()
        # Re-fetched rather than refreshed: refresh_from_db() reads self._state.db, which the
        # branch save set to the branch alias, so it would answer from the branch either way.
        self.assertEqual(CustomScriptProject.objects.get(pk=project.pk).name, 'After')

    def test_a_project_change_raises_no_branch_diff(self):
        branch = self.branch('Diff')
        # Change logging writes no ObjectChange without a current request, so without
        # event_tracking this would pass for a branch-aware model too.
        with activate_branch(branch), event_tracking(self.request):
            CustomScriptProject.objects.create(name='Undiffed', key='undiffed')
            Site.objects.create(name='Diffed Site', slug='diffed-site')
        site_diffs = ChangeDiff.objects.filter(branch=branch, object_type=ObjectType.objects.get_for_model(Site))
        self.assertTrue(site_diffs.exists(), 'no diff for a branch-aware model, so this proves nothing')
        project_diffs = ChangeDiff.objects.filter(
            branch=branch, object_type=ObjectType.objects.get_for_model(CustomScriptProject)
        )
        self.assertFalse(project_diffs.exists())

    def test_a_tag_assignment_inside_a_branch_stays_in_the_branch(self):
        # Both rows exist in main before provisioning, so the branch schema replicates them.
        project = CustomScriptProject.objects.create(name='Tagged', key='tagged')
        tag = Tag.objects.create(name='Branch Tag', slug='branch-tag')
        branch = self.branch('Tags')
        with activate_branch(branch):
            project.tags.add(tag)
            self.assertEqual(list(project.tags.all()), [tag])
        # Branching checks assignments ahead of any exempt list, so no plugin can make them
        # global. This records that boundary rather than asking for it.
        self.assertEqual(list(project.tags.all()), [])

    def test_a_journal_entry_inside_a_branch_stays_in_the_branch(self):
        project = CustomScriptProject.objects.create(name='Journalled', key='journalled')
        object_type = ObjectType.objects.get_for_model(CustomScriptProject)
        entries = JournalEntry.objects.filter(assigned_object_type=object_type, assigned_object_id=project.pk)
        branch = self.branch('Journal')
        with activate_branch(branch):
            JournalEntry.objects.create(
                assigned_object_type=object_type, assigned_object_id=project.pk, comments='Inside'
            )
            self.assertEqual(entries.count(), 1)
        self.assertEqual(entries.count(), 0)
