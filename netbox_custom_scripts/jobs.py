"""Background jobs for the Custom Scripts plugin."""

from django.db import transaction

from core.exceptions import JobFailed
from netbox.jobs import JobRunner

from . import branching
from .storage import config, store
from .storage.exceptions import StorageConfigurationError, StorageError


class ProjectStorageCleanupJob(JobRunner):
    """
    Remove one deleted revision's stored content.

    The database rows are gone by the time this runs, so the job carries everything deletion
    needs: the storage key, the digest, and the manifest's file paths. enqueue_cleanup
    persists that same payload on the Job row, which keeps a durable inventory even when the
    queue loses the task. Deletion is by exact key and tolerates content that is already
    gone, so a run that failed partway can be run again and finishes the remainder. A failure
    lands as a failed Job whose log names what was left behind.
    """

    class Meta:
        name = 'Custom Script Project storage cleanup'

    @classmethod
    def enqueue_cleanup(cls, *, storage_key, digest, paths):
        """
        Enqueue one revision's cleanup with its payload persisted on the Job row.

        RQ kwargs live only in the queue, and the rows naming the content are gone once the
        delete commits, so the Job row's data field keeps the durable copy of what has to be
        removed. Job.enqueue() hands the task to the queue in a commit hook, so saving the
        payload inside the same transaction puts it in place before the queue can run the
        task, and a failed handoff still leaves a pending Job carrying its own inventory.
        Called from the deletion signal, the atomic block nests inside the deleting
        transaction, so the Job and its payload commit or roll back with the rows they clean
        up after, which holds because the signal refuses any alias other than the default.
        """
        payload = {'storage_key': str(storage_key), 'digest': digest, 'paths': list(paths)}
        with transaction.atomic():
            job = cls.enqueue(**payload)
            job.data = payload
            job.save(update_fields=('data',))
        return job

    def run(self, storage_key, digest, paths, **kwargs):
        """Recheck routing safety, then remove the named keys, failing the job on what is left."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = (
                f'Refusing storage cleanup and leaving source in the store, because {reason} '
                f'Removing it could take away content another schema still serves: {storage_key} {digest}'
            )
            self.logger.error(detail)
            raise JobFailed()
        try:
            storage = config.get_storage()
            store.delete_revision(storage, storage_key, digest, paths)
        except (OSError, StorageError, StorageConfigurationError) as error:
            # An unreachable backend or a missing storage entry is an expected operational
            # failure. The job log carries the detail, and the failed status plus the payload
            # persisted in data are what an operator or a future reconciler retries from. The
            # message is rendered up front, because the job log records it verbatim rather
            # than interpolating lazy logging arguments.
            detail = f'Storage cleanup left content in the store: {storage_key} {digest}: {error}'
            self.logger.error(detail)
            raise JobFailed() from error
