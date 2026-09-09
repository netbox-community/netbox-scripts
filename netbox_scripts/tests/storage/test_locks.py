import uuid
import warnings
from unittest import mock

from django.db import DEFAULT_DB_ALIAS, OperationalError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase

from netbox_scripts.storage.locks import ADVISORY_LOCK_NAMESPACE, advisory_key, project_lock, project_write_lock

INT4_MIN = -(2**31)
INT4_MAX = 2**31 - 1


def _signed(value):
    """Reinterpret one pg_locks key column as the signed integer it was locked with."""
    # classid and objid are oid, which is unsigned, so a negative key comes back offset by
    # 2**32. Half of all derived keys are negative, so comparing without this is flaky.
    return value - 2**32 if value >= 2**31 else value


def held_locks():
    """Return the two-integer advisory keys this session currently holds."""
    with connection.cursor() as cursor:
        # objsubid distinguishes the two keyspaces: 1 is a single bigint key, 2 is a pair of
        # integers. Filtering on 2 keeps NetBox's own single-key locks out of the result.
        cursor.execute(
            'SELECT classid, objid FROM pg_locks '
            'WHERE locktype = %s AND objsubid = 2 AND pid = pg_backend_pid() AND granted',
            ('advisory',),
        )
        return [(_signed(classid), _signed(objid)) for classid, objid in cursor.fetchall()]


def _force_unlock(namespace, key):
    """Release a key whose own unlock was made to fail."""
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s, %s)', (namespace, key))


class AdvisoryKeyTestCase(TestCase):
    """The derivation of one project's lock key from its storage key."""

    def test_key_is_stable_for_one_storage_key(self):
        storage_key = uuid.uuid4()
        self.assertEqual(advisory_key(storage_key), advisory_key(storage_key))

    def test_key_accepts_the_string_form_of_the_same_uuid(self):
        storage_key = uuid.uuid4()
        self.assertEqual(advisory_key(storage_key), advisory_key(str(storage_key)))

    def test_distinct_projects_get_distinct_keys(self):
        keys = {advisory_key(uuid.uuid4()) for _ in range(200)}
        self.assertEqual(len(keys), 200)

    def test_namespace_is_the_first_of_the_pair(self):
        namespace, _ = advisory_key(uuid.uuid4())
        self.assertEqual(namespace, ADVISORY_LOCK_NAMESPACE)

    def test_both_members_fit_a_signed_32_bit_integer(self):
        # The two-key advisory lock functions take int4 arguments. A value outside the range
        # would be rejected by PostgreSQL rather than truncated.
        for _ in range(200):
            namespace, key = advisory_key(uuid.uuid4())
            self.assertTrue(INT4_MIN <= namespace <= INT4_MAX)
            self.assertTrue(INT4_MIN <= key <= INT4_MAX)


class ProjectLockTestCase(TestCase):
    """Acquisition and release of the project lock."""

    def test_lock_is_held_inside_the_block_and_released_after(self):
        storage_key = uuid.uuid4()
        expected = advisory_key(storage_key)
        with project_lock(storage_key):
            self.assertIn(expected, held_locks())
        self.assertNotIn(expected, held_locks())

    def test_release_runs_when_the_block_raises(self):
        storage_key = uuid.uuid4()
        expected = advisory_key(storage_key)
        with self.assertRaises(RuntimeError), project_lock(storage_key):
            raise RuntimeError('the block failed')
        self.assertNotIn(expected, held_locks())

    def test_nesting_the_same_key_releases_symmetrically(self):
        # PostgreSQL counts session-level acquisitions, so the outer hold has to survive the
        # inner release. Staging reached from a caller that already holds the lock relies on it.
        storage_key = uuid.uuid4()
        expected = advisory_key(storage_key)
        with project_lock(storage_key):
            with project_lock(storage_key):
                self.assertIn(expected, held_locks())
            self.assertIn(expected, held_locks())
        self.assertNotIn(expected, held_locks())

    def test_two_projects_are_locked_independently(self):
        first, second = uuid.uuid4(), uuid.uuid4()
        with project_lock(first):
            self.assertIn(advisory_key(first), held_locks())
            self.assertNotIn(advisory_key(second), held_locks())

    def test_alias_defaults_to_the_default_connection(self):
        storage_key = uuid.uuid4()
        with project_lock(storage_key, using=DEFAULT_DB_ALIAS):
            self.assertIn(advisory_key(storage_key), held_locks())

    def test_a_failed_release_leaves_the_blocks_exception_in_place(self):
        storage_key = uuid.uuid4()
        namespace, key = advisory_key(storage_key)
        conn = connections[DEFAULT_DB_ALIAS]
        # Only the release is made to fail, so the acquire really takes the lock and this
        # session would hold it past the test.
        self.addCleanup(_force_unlock, namespace, key)
        cursors = [conn.cursor(), OperationalError('the connection dropped')]

        with warnings.catch_warnings(record=True) as raised:
            warnings.simplefilter('always')
            # Exit order is right to left, so the lock releases while assertRaises is still
            # watching and catch_warnings is still recording.
            with (
                mock.patch.object(conn, 'cursor', side_effect=cursors),
                self.assertRaises(RuntimeError) as caught,
                project_lock(storage_key),
            ):
                raise RuntimeError('the block failed')

        self.assertEqual(str(caught.exception), 'the block failed')
        self.assertTrue(any('failed to release' in str(entry.message) for entry in raised))

    def test_a_failed_release_after_a_successful_block_warns_and_does_not_raise(self):
        storage_key = uuid.uuid4()
        namespace, key = advisory_key(storage_key)
        conn = connections[DEFAULT_DB_ALIAS]
        self.addCleanup(_force_unlock, namespace, key)
        cursors = [conn.cursor(), OperationalError('the connection dropped')]

        with warnings.catch_warnings(record=True) as raised:
            warnings.simplefilter('always')
            with mock.patch.object(conn, 'cursor', side_effect=cursors), project_lock(storage_key):
                pass

        # The caller is told nothing beyond the warning, so the lock stays held until the
        # connection closes. Documented on project_lock rather than routed to a job log.
        self.assertTrue(any('failed to release' in str(entry.message) for entry in raised))
        self.assertIn(advisory_key(storage_key), held_locks())


class ProjectWriteLockTestCase(TransactionTestCase):
    """A database writer excludes storage readers until the outer transaction ends."""

    @staticmethod
    def other_session_can_lock(storage_key):
        connection = connections.create_connection(DEFAULT_DB_ALIAS)
        connection.ensure_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', advisory_key(storage_key))
                acquired = cursor.fetchone()[0]
                if acquired:
                    cursor.execute('SELECT pg_advisory_unlock(%s, %s)', advisory_key(storage_key))
            return acquired
        finally:
            connection.close()

    def test_an_inner_success_keeps_the_lock_until_outer_commit(self):
        key = uuid.uuid4()
        with transaction.atomic():
            with project_write_lock(key):
                self.assertFalse(self.other_session_can_lock(key))
            self.assertFalse(self.other_session_can_lock(key))
        self.assertTrue(self.other_session_can_lock(key))

    def test_an_outer_rollback_releases_the_lock(self):
        key = uuid.uuid4()
        with self.assertRaises(RuntimeError), transaction.atomic():
            with project_write_lock(key):
                self.assertFalse(self.other_session_can_lock(key))
            raise RuntimeError('refused write')
        self.assertTrue(self.other_session_can_lock(key))

    def test_a_session_lock_and_a_write_lock_nest_on_one_key(self):
        # The shape ingestion uses: project_lock spans the read and the stage, and the declaration
        # takes project_write_lock on the same key inside it. Each releases on its own terms.
        key = uuid.uuid4()
        with project_lock(key):
            self.assertFalse(self.other_session_can_lock(key))
            with transaction.atomic(), project_write_lock(key):
                self.assertFalse(self.other_session_can_lock(key))
            self.assertFalse(self.other_session_can_lock(key))
        self.assertTrue(self.other_session_can_lock(key))
