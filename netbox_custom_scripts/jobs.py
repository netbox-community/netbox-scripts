"""Background jobs for the Custom Scripts plugin."""

from django.db import transaction
from rq.timeouts import JobTimeoutException

from core.exceptions import JobFailed
from netbox.jobs import JobRunner

from . import activation, branching
from .choices import ActivationPolicyChoices, RevisionStatusChoices
from .constants import VALIDATION_JOB_TIMEOUT
from .execution import ScriptNotExecutableError, run_script
from .models import CustomScript, CustomScriptProjectRevision
from .runtime.exceptions import (
    DiscoveryError,
    EntrypointImportError,
    InvalidModulePathError,
    ScriptMetadataError,
    ScriptResolutionError,
)
from .runtime.loader import revision_import_session, unload_revision
from .runtime.resolution import resolve_script_class
from .storage import config, store
from .storage.exceptions import ActivationError, RevisionCorruptError, StorageConfigurationError, StorageError
from .storage.locks import project_lock
from .storage.service import require_default_database
from .validation import ValidationStateError, build_error_sanitizer, validate_revision

# Everything that means "this revision cannot give us the class the row names". Each one is a
# statement about content or configuration, so a run fails rather than being retried blindly.
RESOLUTION_FAILURES = (
    DiscoveryError,
    EntrypointImportError,
    InvalidModulePathError,
    RevisionCorruptError,
    ScriptMetadataError,
    ScriptResolutionError,
)


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


class CustomScriptJob(JobRunner):
    """
    Run one Custom Script against the revision its enqueue pinned.

    The revision is fixed when the run is requested, not when the worker picks it up, so a
    queued run executes the source the operator was looking at even if the project has moved
    on since. The pin is recorded on the Job row as well as passed to the worker, which is what
    makes a finished Job say what it ran rather than only what it was called.

    Everything the run needs from the tree is read through the runtime tier, so the source is
    materialized and verified against its manifest before any of it is imported, and the
    revision is unloaded afterwards. Each run therefore imports fresh and module-level state
    cannot carry from one run into the next.

    Declared pip requirements are not checked, that is a later workstream.
    """

    class Meta:
        name = 'Run Custom Script'

    @classmethod
    def enqueue_run(cls, script, *, data, commit, request=None, user=None, **kwargs):
        """
        Enqueue one run of a Custom Script, pinned to the revision its project serves now.

        The pinned identity is saved on the Job row inside the enqueueing transaction, so the
        queue can never run a task whose record of what it runs is missing. The script's own
        recorded metadata supplies the job timeout and the notification policy. Raises
        ScriptNotExecutableError when the script cannot run, which covers a disabled or retired
        script, a disabled project, and a project serving no revision.
        """
        branching.require_safe_routing()
        if not script.is_executable:
            raise ScriptNotExecutableError(
                f'"{script}" cannot be run right now. It is disabled, retired, or its project '
                'is disabled or is not serving a revision.'
            )
        revision = script.project.active_revision
        payload = {
            'revision_id': revision.pk,
            'revision_digest': revision.digest,
            'module_path': script.module_path,
            'class_name': script.class_name,
            'commit': bool(commit),
        }
        # Input values are deliberately absent from the payload. Variables resolve to model
        # instances and uploaded files, so they are not JSON, and rendering them for the row
        # would need a policy on values an author may not want recorded.
        if script.job_timeout:
            kwargs.setdefault('job_timeout', script.job_timeout)
        kwargs.setdefault('notifications', script.notifications_default)
        with transaction.atomic():
            job = cls.enqueue(instance=script, user=user, data=data, request=request, **payload, **kwargs)
            # An immediate run has already finished and recorded its result by the time enqueue()
            # returns, so the pin goes underneath whatever is there rather than over it.
            job.data = {**payload, **(job.data or {})}
            job.save(update_fields=('data',))
        return job

    def run(self, *, revision_id, revision_digest, module_path, class_name, data, commit, request=None, **kwargs):
        """Resolve the pinned class out of its revision and run it, recording the result."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing to run a Custom Script, because {reason}')
            raise JobFailed()
        # Enabled is the administrator's field, so turning it off has to stop a run that was
        # already queued. The pinned revision is deliberately not rechecked: the point of
        # pinning is that a run executes the source it was requested against.
        script = CustomScript.objects.filter(pk=self.job.object_id).first()
        if script is not None and not (script.enabled and script.project.enabled):
            self.logger.error(f'"{script}" was disabled after this run was requested, so it was not run.')
            raise JobFailed()
        revision = CustomScriptProjectRevision.objects.filter(pk=revision_id).first()
        if revision is None:
            self.logger.error(
                f'The revision this run was pinned to no longer exists, so {module_path}.{class_name} '
                'cannot be run as it was requested.'
            )
            raise JobFailed()

        storage_key = str(revision.project.storage_key)
        sanitize = build_error_sanitizer(storage_key, revision.digest)
        self.logger.info(f'Running {module_path}.{class_name} from revision {revision_digest[:12]}.')
        try:
            with revision_import_session(storage_key, revision.digest):
                try:
                    script_class = self._resolve(revision, storage_key, module_path, class_name, sanitize)
                    self._run_class(
                        script_class, data=data, commit=commit, request=request, revision=revision, sanitize=sanitize
                    )
                finally:
                    unload_revision(storage_key, revision.digest)
        except (StorageError, StorageConfigurationError, OSError) as error:
            detail = sanitize(f'The run could not reach the source it was pinned to: {error}')
            self.logger.error(detail)
            raise JobFailed() from error
        except Exception as error:
            # The run log already carries the detail, so this line only fails the Job.
            self.logger.error(sanitize(f'The Custom Script did not finish: {error}'))
            raise JobFailed() from error

    def _resolve(self, revision, storage_key, module_path, class_name, sanitize):
        """Return the pinned class, failing the Job when this revision cannot supply it."""
        try:
            return resolve_script_class(
                storage_key,
                revision.digest,
                discovered_scripts=revision.discovered_scripts,
                project_key=revision.project.key,
                module_path=module_path,
                class_name=class_name,
                storage=config.get_storage(),
                manifest=revision.manifest,
                passthrough=(JobTimeoutException,),
            )
        except RESOLUTION_FAILURES as error:
            detail = sanitize(f'Revision {revision.digest[:12]} cannot supply {module_path}.{class_name}: {error}')
            self.logger.error(detail)
            raise JobFailed() from error

    def _run_class(self, script_class, *, data, commit, request, revision, sanitize):
        """Run one resolved class, recording its log and output on the Job either way."""
        instance = script_class()
        instance.request = request
        # A variable of the FileVar kind is bound in the upload rather than in the posted data,
        # so the two halves of the form are put back together here.
        values = dict(data)
        for name, uploaded in getattr(request, 'FILES', {}).items():
            values[name] = uploaded
        try:
            run_script(instance, data=values, commit=commit, request=request)
        finally:
            # The result joins the pin rather than replacing it, so a finished Job still says
            # which revision and which class it ran, not only what came out.
            self.job.data = {
                **(self.job.data or {}),
                **_sanitized_run_record(instance, sanitize),
                'revision_digest': revision.digest,
            }


def _sanitized_run_record(instance, sanitize):
    """Return one run's log and output with the revision's runtime identities stripped out."""
    # A traceback names the file it was raised in, and that file lives in the runtime cache
    # under the storage key and digest, so an unhandled exception puts both in the record an
    # operator reads. The run context builds the log and knows nothing about storage, which is
    # why the stripping belongs here.
    record = instance.get_job_data()
    return {
        'log': [{**entry, 'message': sanitize(entry.get('message'))} for entry in record.get('log', [])],
        'output': sanitize(record['output']) if isinstance(record.get('output'), str) else record.get('output'),
    }
