"""
Integration tests against a real provisioned NetBox Branching branch.

Every test is skipped when NetBox Branching is absent, so the default suite is unchanged. The cases
are TransactionTestCase: a branch lives in its own PostgreSQL schema on its own connection, which no
savepoint rolls back.
"""

import hashlib
import os
import time
import unittest
import uuid
from unittest import mock

import django_rq
from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TransactionTestCase
from django.urls import reverse

from core.models import ObjectType
from dcim.models import Site
from extras.models import JournalEntry, Tag
from netbox.context_managers import event_tracking
from netbox_custom_scripts import signals
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.models import CustomScriptProject, CustomScriptProjectRevision
from netbox_custom_scripts.storage import config, store
from netbox_custom_scripts.storage.manifest import compute_digest
from netbox_custom_scripts.storage.paths import revision_prefix

# Branching can be importable while absent from INSTALLED_APPS, and defining one of its models in
# that state raises RuntimeError, which no ImportError guard would catch.
HAS_BRANCHING = apps.is_installed('netbox_branching')

if HAS_BRANCHING:
    from netbox_branching.choices import BranchMergeStrategyChoices, BranchStatusChoices
    from netbox_branching.models import Branch, ChangeDiff
    from netbox_branching.provisioning import quote_ident
    from netbox_branching.utilities import activate_branch, get_tables_to_replicate

User = get_user_model()

# One tiny tree. The digest comes from the production helper so the constant cannot drift.
SOURCE = {'hello.py': b'print("hi")\n'}
MANIFEST = [
    {
        'path': 'hello.py',
        'size': len(SOURCE['hello.py']),
        'sha256': hashlib.sha256(SOURCE['hello.py']).hexdigest(),
    }
]
DIGEST = compute_digest(MANIFEST)

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

    def staged_revision(self, project, entrypoint_digest=''):
        """Return one VALID revision of a project, with its single source file really stored."""
        store.write_revision(config.get_storage(), project.storage_key, DIGEST, SOURCE, MANIFEST)
        return CustomScriptProjectRevision.objects.create(
            project=project,
            digest=DIGEST,
            status=RevisionStatusChoices.VALID,
            manifest=MANIFEST,
            entrypoint_digest=entrypoint_digest,
        )

    def revision_stored(self, project):
        """Report whether the stored tree behind DIGEST is still in the backend."""
        return config.get_storage().exists(f'{revision_prefix(project.storage_key, DIGEST)}hello.py')


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


class BranchDeletionTestCase(BranchingTestCase):
    def setUp(self):
        super().setUp()
        # This class commits, so the deletion signal really enqueues. Drained to leave it as found.
        self.addCleanup(django_rq.get_queue('default').empty)
        self.project = CustomScriptProject.objects.create(name='Deletions', key='deletions')

    def test_a_revision_deleted_inside_a_branch_is_gone_from_main(self):
        revision = self.staged_revision(self.project)
        # delete() clears the instance pk, so a later filter on it finds nothing whatever routing did.
        pk = revision.pk
        branch = self.branch('Delete')
        with activate_branch(branch):
            revision.delete()
        self.assertFalse(CustomScriptProjectRevision.objects.filter(pk=pk).exists())

    def test_reverting_a_merge_does_not_resurrect_a_deleted_revision(self):
        # A resurrected row would name bytes the cleanup job already reclaimed, which is the
        # unrecoverable shape: a revision whose content is gone.
        revision = self.staged_revision(self.project)
        pk = revision.pk
        branch = self.branch('Revert', merge_strategy=BranchMergeStrategyChoices.ITERATIVE)
        # The Site is a branch-aware companion, and it carries the controls below. Without one the
        # branch holds no changes at all and both operations return before doing anything.
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.create(name='Reverted Site', slug='reverted-site')
            revision.delete()
        sites = Site.objects.filter(slug='reverted-site')
        self.assertFalse(sites.exists(), 'a branch-aware row reached main before the merge')
        # Only a merged branch can be reverted, so this merge is a precondition, not the subject.
        branch.merge(user=self.user)
        self.assertTrue(sites.exists(), 'the merge applied nothing, so the revert proves nothing')
        branch.revert(user=self.user)
        self.assertFalse(sites.exists())
        self.assertFalse(CustomScriptProjectRevision.objects.filter(pk=pk).exists())

    def test_a_branch_delete_hands_off_cleanup_for_unreferenced_content(self):
        # Asserted on the handoff, not on the bytes: only the job removes content, and it never
        # runs here, so a storage assertion would hold whatever the signal decided.
        revision = self.staged_revision(self.project)
        branch = self.branch('Cleanup')
        with mock.patch.object(signals.ProjectStorageCleanupJob, 'enqueue_cleanup') as enqueue, activate_branch(branch):
            revision.delete()
        enqueue.assert_called_once_with(storage_key=self.project.storage_key, digest=DIGEST, paths=['hello.py'])

    def test_a_branch_delete_withholds_cleanup_for_content_a_sibling_names(self):
        # Two rows share one stored tree, so deleting one must not hand off bytes the other names.
        first = self.staged_revision(self.project)
        self.staged_revision(self.project, entrypoint_digest='b' * 64)
        branch = self.branch('Shared')
        with mock.patch.object(signals.ProjectStorageCleanupJob, 'enqueue_cleanup') as enqueue, activate_branch(branch):
            first.delete()
        enqueue.assert_not_called()
        self.assertTrue(self.revision_stored(self.project))


class MergeAndRevertTestCase(BranchingTestCase):
    def test_merging_a_branch_does_not_replay_a_project_change(self):
        branch = self.branch('Merge', merge_strategy=BranchMergeStrategyChoices.ITERATIVE)
        # The Site is a branch-aware companion. Without one the branch holds no changes and
        # merge() returns before doing anything, which would prove nothing.
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.create(name='Merged Site', slug='merged-site')
            CustomScriptProject.objects.create(name='Merged', key='merged')
        sites = Site.objects.filter(slug='merged-site')
        projects = CustomScriptProject.objects.filter(key='merged')
        # The discriminating pair: the global row is already in main while the branch-aware one
        # waits for the merge.
        self.assertEqual(projects.count(), 1)
        self.assertFalse(sites.exists())
        branch.merge(user=self.user)
        self.assertTrue(sites.exists(), 'the merge applied nothing, so the count below proves nothing')
        # A replay would apply the project a second time.
        self.assertEqual(projects.count(), 1)

    def test_reverting_a_merge_does_not_undo_a_project_edit(self):
        project = CustomScriptProject.objects.create(name='Before', key='reverted-edit')
        branch = self.branch('RevertEdit', merge_strategy=BranchMergeStrategyChoices.ITERATIVE)
        with activate_branch(branch), event_tracking(self.request):
            Site.objects.create(name='Undone Site', slug='undone-site')
            project.name = 'After'
            project.save()
        sites = Site.objects.filter(slug='undone-site')
        branch.merge(user=self.user)
        self.assertTrue(sites.exists(), 'the merge applied nothing')
        branch.revert(user=self.user)
        self.assertFalse(sites.exists(), 'the revert undid nothing, so the name below proves nothing')
        # Re-fetched rather than refreshed, which would answer from the branch the save recorded.
        self.assertEqual(CustomScriptProject.objects.get(pk=project.pk).name, 'After')
