"""
Project-scoped serialization for the storage lifecycle.

Every operation that reads or writes one project's stored content takes the lock this module
hands out, keyed by the project's immutable storage_key. Staging, script file refresh,
activation, the validation service's row transitions, and physical cleanup therefore observe
one another's writes in a defined order instead of interleaving inside a store the database
cannot see.

The lock is a PostgreSQL session-level advisory lock rather than a row lock or a lease. A row
lock would hold a transaction open across a remote conversation with the object store, which
is exactly what activation goes out of its way to avoid, and a lease would add a second
reclaim timer beside the validation lease already in the revision row. A session lock is
released when the connection drops, so a pod killed without warning frees its own claim.

Two consequences a caller has to know. The lock is not transactional, so a rollback does not
release it and only leaving the block does. It is also counted by PostgreSQL, so a nested
acquisition of the same key succeeds and needs its own release, which makes the context
manager safe to nest.

A session lock belongs to the physical backend connection, so transaction-mode pooling breaks
it. That requirement is stated with the other deployment ones in docs/configuration.md.
"""

import hashlib

from django.db import DEFAULT_DB_ALIAS
from django_pg_utils import advisory_lock

__all__ = (
    'ADVISORY_LOCK_NAMESPACE',
    'advisory_key',
    'project_lock',
)

# The first of the two integers every project lock is taken on, arbitrary and recognisable in
# query logs. models/migration.py derives the next namespace up from it. Core registers its own
# keys in ADVISORY_LOCK_KEYS (netbox/constants.py) and hashes per-tree ones in utilities/ltree.py,
# and none of them is 770100 or 770101. The separation is numeric and not by arity: extras/jobs.py
# already takes custom-field-data as the two-integer pair (115100, pk).
ADVISORY_LOCK_NAMESPACE = 770100


def advisory_key(storage_key):
    """
    Return the integer pair one project's lock is taken on.

    The second integer is truncated to fit a signed 32-bit integer, which is what the two-key
    advisory lock functions accept.
    """
    # Keyed on the storage_key rather than the primary key: it is immutable, so the lock stays
    # put across a rename and nothing an author edits can move it.
    digest = hashlib.sha256(str(storage_key).encode()).digest()
    return ADVISORY_LOCK_NAMESPACE, int.from_bytes(digest[:4], byteorder='big', signed=True)


def project_lock(storage_key, *, using=DEFAULT_DB_ALIAS):
    """
    Hold the serialization lock for one project's stored content.

    Acquisition waits for as long as another holder keeps the lock. The release runs on both
    paths and never raises, it warns: a failure after the block raised leaves that exception in
    place, and a failure after the block succeeded leaves the lock held for the life of the
    connection.
    """
    return advisory_lock(advisory_key(storage_key), using=using)
