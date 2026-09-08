"""Serialization of migration service entry points."""

from functools import wraps

from ..models.migration import migration_lock


def serialized_migration_step(function):
    """Run one service step against fresh state under the installation-wide migration lock."""

    @wraps(function)
    def serialized(run, *args, **kwargs):
        with migration_lock():
            if run is not None:
                run.refresh_from_db()
            return function(run, *args, **kwargs)

    return serialized
