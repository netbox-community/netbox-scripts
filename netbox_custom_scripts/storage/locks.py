"""
Project-scoped serialization for the storage lifecycle.

Every operation that reads or writes one project's stored content takes the lock this module
hands out, keyed by the project's immutable storage_key. Staging, entrypoint refresh,
activation, the validation service's row transitions, and physical cleanup therefore observe
one another's writes in a defined order instead of interleaving inside a store the database
cannot see.

The lock is a PostgreSQL session-level advisory lock rather than a row lock or a lease. A row
lock would hold a transaction open across a remote conversation with the object store, which
is exactly what activation goes out of its way to avoid, and a lease would add a second
reclaim timer beside the validation lease already in the revision row. A session lock is
released when the connection drops, so a pod killed without warning frees its own claim.

This module owns its primitive rather than importing NetBox's advisory_lock. At the 4.7 floor
that is a removal candidate, not a settled divergence.

Two consequences a caller has to know. The lock is not transactional, so a rollback does not
release it and every acquisition needs its release in a finally. It is also counted by
PostgreSQL, so a nested acquisition of the same key succeeds and needs its own release, which
makes the context manager safe to nest.

A session lock belongs to the physical backend connection, so transaction-mode pooling breaks
it. That requirement is stated with the other deployment ones in docs/configuration.md.
"""

import hashlib
from contextlib import contextmanager

from django.db import DEFAULT_DB_ALIAS, connections

__all__ = (
    'ADVISORY_LOCK_NAMESPACE',
    'advisory_key',
    'project_lock',
)

# The first of the two integers every project lock is taken on. PostgreSQL keeps the
# one-bigint and two-integer advisory keyspaces disjoint, so a two-integer key cannot collide
# with NetBox itself, which takes single-key locks throughout. The value is arbitrary and
# exists to be recognisable in query logs.
ADVISORY_LOCK_NAMESPACE = 770100


def advisory_key(storage_key):
    """
    Return the integer pair one project's lock is taken on.

    The second integer is derived from the storage_key rather than the primary key, because
    storage_key is immutable and names the content itself, so the lock stays put across a
    rename and cannot be moved by anything an author edits. It is truncated to fit a signed
    32-bit integer, which is what the two-key advisory lock functions accept.
    """
    digest = hashlib.sha256(str(storage_key).encode()).digest()
    return ADVISORY_LOCK_NAMESPACE, int.from_bytes(digest[:4], byteorder='big', signed=True)


@contextmanager
def project_lock(storage_key, *, using=DEFAULT_DB_ALIAS):
    """
    Hold the serialization lock for one project's stored content.

    Acquisition waits for as long as another holder keeps the lock. The release runs even when
    the block raised, because a session lock outlives the failed transaction that would
    otherwise have carried it away.
    """
    namespace, key = advisory_key(storage_key)
    with connections[using].cursor() as cursor:
        cursor.execute('SELECT pg_advisory_lock(%s, %s)', (namespace, key))
    try:
        yield
    finally:
        with connections[using].cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s, %s)', (namespace, key))
