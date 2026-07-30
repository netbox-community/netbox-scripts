"""Background jobs for the Custom Scripts plugin."""

from django.db import transaction
from rq.timeouts import JobTimeoutException

from core.exceptions import JobFailed
from netbox.jobs import JobRunner

from . import activation, branching
from .choices import ActivationPolicyChoices, RevisionStatusChoices
from .constants import VALIDATION_JOB_TIMEOUT
from .models import CustomScriptProjectRevision
from .runtime.exceptions import EntrypointImportError
from .storage import config, store
from .storage.exceptions import ActivationError, StorageConfigurationError, StorageError
from .storage.locks import project_lock
from .storage.service import require_default_database
from .validation import ValidationStateError, build_error_sanitizer, validate_revision


class ProjectStorageCleanupJob(JobRunner):
    """
    Remove one deleted revision's stored content.

    The database rows are gone by the time this runs, so the job carries everything deletion
    needs: the storage key, the digest, and the manifest's file paths. enqueue_cleanup
    persists that same payload on the Job row, which keeps a durable inventory even when the
    queue loses the task. Deletion is by exact key and tolerates content that is already
    gone, so a run that failed partway can be run again and finishes the remainder. A failure
    lands as a failed Job whose log names what was left behind.

    The reference recheck and the removal happen under the project lock, so this job is the
    one place that decides whether stored content is still claimed. Deletion itself takes no
    lock, which is why the decision has to be made here rather than trusted from the delete.
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
        """Recheck routing safety and references, then remove the named keys, failing on what is left."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = (
                f'Refusing storage cleanup and leaving source in the store, because {reason} '
                f'Removing it could take away content another schema still serves: {storage_key} {digest}'
            )
            self.logger.error(detail)
            raise JobFailed()
        # The recheck and the removal it authorizes have to be one indivisible step. A staging
        # call that creates a referencing row between them would otherwise have its content
        # deleted out from under it, which is why the project lock covers both and why staging
        # takes the same lock across its write.
        with project_lock(storage_key):
            # The digest can be re-staged under a new entrypoint configuration between the
            # delete that recorded this job and this run. Content a current revision references
            # is left in place and the run succeeds, since there is nothing left to reclaim.
            if CustomScriptProjectRevision.objects.filter(project__storage_key=storage_key, digest=digest).exists():
                self.logger.info(
                    f'Leaving stored content in place, a current revision references it again: {storage_key} {digest}'
                )
                return
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


class RevisionValidationJob(JobRunner):
    """
    Drive one revision to a validation verdict inside a worker.

    Imports run in this process, the worker is the isolated execution environment the
    concept prescribes, and the rq job timeout bounds a run while the longer lease in the
    revision row hands the claim on if this worker dies without a trace. Environment
    trouble fails the job and leaves the revision claimable again, a verdict is recorded
    by the validation service itself.
    """

    class Meta:
        name = 'Custom Script Revision validation'

    @classmethod
    def enqueue_validation(cls, revision, **kwargs):
        """
        Enqueue one revision's validation with the revision pk persisted on the Job row.

        The atomic block nests inside any caller transaction, so the Job and its payload
        commit or roll back with whatever staged the revision, and the queue handoff in
        Job.enqueue()'s commit hook can never run a task whose payload is missing. The rq
        job timeout travels with the enqueue, it stays below the reclaim lease by the
        margin constants.py documents.
        """
        branching.require_safe_routing()
        require_default_database(revision)
        payload = {'revision_pk': revision.pk}
        with transaction.atomic():
            job = cls.enqueue(job_timeout=VALIDATION_JOB_TIMEOUT, **payload, **kwargs)
            job.data = payload
            job.save(update_fields=('data',))
        return job

    def run(self, revision_pk=None, **kwargs):
        """Recheck routing safety, then validate, failing the job on anything but a verdict."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = f'Refusing revision validation, because {reason}'
            self.logger.error(detail)
            raise JobFailed()
        revision = CustomScriptProjectRevision.objects.filter(pk=revision_pk).first()
        if revision is None:
            self.logger.info(f'Revision {revision_pk} no longer exists, nothing to validate.')
            return
        try:
            revision = validate_revision(revision, job=self.job, passthrough=(JobTimeoutException,))
        except ValidationStateError as error:
            self.logger.error(str(error))
            raise JobFailed() from error
        except (EntrypointImportError, StorageError, OSError) as error:
            # An expected environment failure: the claim was rolled back, the revision is
            # claimable again, and the failed job carries the sanitized reason. The message
            # is rendered up front, because the job log records it verbatim.
            sanitize = build_error_sanitizer(str(revision.project.storage_key), revision.digest)
            detail = sanitize(f'Revision validation could not complete and needs another run: {error}')
            self.logger.error(detail)
            raise JobFailed() from error
        if revision.status == RevisionStatusChoices.INVALID:
            self.logger.warning(f'The revision is invalid, {len(revision.validation_errors)} problem(s) recorded.')
            return
        self.logger.info(f'The revision validated as {revision.status}.')
        self._activate_if_policy_allows(revision)

    def _activate_if_policy_allows(self, revision):
        """
        Promote a valid revision when its project asked for automatic activation.

        The verdict is already recorded and correct, so a refused or failed activation fails the
        job without touching it. The project keeps serving whatever it served before, which is
        the outcome the concept requires of a validation that cannot complete its last step.
        """
        if revision.project.activation_policy != ActivationPolicyChoices.AUTOMATIC_IF_VALID:
            self.logger.info('Leaving activation to an operator, this project activates manually.')
            return
        try:
            activation.activate_revision(revision)
        except (ActivationError, StorageError, OSError) as error:
            sanitize = build_error_sanitizer(str(revision.project.storage_key), revision.digest)
            detail = sanitize(f'The revision validated but could not be activated: {error}')
            self.logger.error(detail)
            raise JobFailed() from error
        self.logger.info('The revision is now the active revision of its project.')
