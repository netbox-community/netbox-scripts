"""Background jobs for the Custom Scripts plugin."""

import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from rq.timeouts import JobTimeoutException

from core.choices import JobIntervalChoices, JobStatusChoices
from core.exceptions import JobFailed
from core.models import Job, ObjectType
from netbox.jobs import JobRunner, system_job
from utilities.rqworker import get_queue_for_model

from . import activation, branching
from .choices import ActivationPolicyChoices, MigrationStateChoices, RevisionStatusChoices
from .constants import ACTIVATABLE_REVISION_STATUSES, STALLED_CLEANUP_GRACE_SECONDS, VALIDATION_JOB_TIMEOUT
from .execution import RESOLUTION_FAILURES, ScriptNotExecutableError, run_script
from .models import CustomScript, CustomScriptProject, CustomScriptProjectRevision, MigrationRun
from .models.migration import migration_lock
from .runtime.exceptions import EntrypointImportError
from .runtime.loader import revision_import_session, unload_revision
from .runtime.resolution import resolve_script_class
from .storage import config, service, store
from .storage.exceptions import ActivationError, StorageConfigurationError, StorageError
from .storage.locks import project_lock
from .storage.service import require_default_database
from .validation import ValidationStateError, build_error_sanitizer, validate_revision


class ProjectStorageCleanupJob(JobRunner):
    """
    Remove one deleted revision's stored content.

    The database rows are gone by the time this runs, so the job carries everything deletion
    needs: the storage key, the digest, and the manifest's file paths. Deletion is by exact key
    and tolerates content that is already gone, so a run that failed partway finishes the
    remainder. A failure lands as a failed Job whose log names what was left behind. The
    reference recheck and the removal both happen under the project lock.
    """

    class Meta:
        name = 'Custom Script Project storage cleanup'

    @classmethod
    def enqueue_cleanup(cls, *, storage_key, digest, paths):
        """
        Enqueue one revision's cleanup with its payload persisted on the Job row.

        The Job row keeps the durable copy of what has to be removed, so a lost queue task still
        leaves a pending Job carrying its own inventory. The atomic block nests inside any caller
        transaction, so the Job and its payload commit or roll back together.
        """
        payload = {'storage_key': str(storage_key), 'digest': digest, 'paths': list(paths)}
        # Not only in RQ kwargs: those live in the queue, and the naming rows go with the delete.
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


@system_job(interval=JobIntervalChoices.INTERVAL_DAILY)
class ProjectStorageSweepJob(JobRunner):
    """
    Report revision content an unfinished cleanup left in the store, reclaiming nothing.

    Rechecks each stalled cleanup under the project lock and records four groups on its own
    Job row: stranded, reclaimed, referenced and unreadable. Scoped to content a cleanup Job
    names.
    """

    class Meta:
        name = 'Custom Script Project storage sweep'

    def run(self, **kwargs):
        """Classify every stalled cleanup, record the report, and log what is stranded."""
        grouped, unreadable = self._candidates()
        report = {'stranded': [], 'reclaimed': [], 'referenced': [], 'unreadable': unreadable}
        # Bound to RQ_DEFAULT_TIMEOUT, which a system job cannot raise, so the row is given the
        # report before the loop and the dict is mutated in place. A pass killed partway is
        # terminated with a full save, which then persists whatever it had classified.
        self.job.data = report
        for (storage_key, digest), candidate in grouped.items():
            self._classify(storage_key, digest, candidate, report)
        for entry in report['stranded']:
            self.logger.warning(
                f'Stranded revision content, {len(entry["paths"])} file(s) from cleanup Job(s) '
                f'{entry["jobs"]}: {entry["storage_key"]} {entry["digest"]}'
            )
        self.logger.info(
            f'{len(report["stranded"])} stranded, {len(report["reclaimed"])} already reclaimed, '
            f'{len(report["referenced"])} referenced again, {len(report["unreadable"])} unreadable.'
        )

    @staticmethod
    def _stalled_cleanups():
        """Return every cleanup Job that has not finished reclaiming the content it names."""
        cutoff = timezone.now() - timedelta(seconds=STALLED_CLEANUP_GRACE_SECONDS)
        # An enqueued cleanup gets the grace period, since it may be waiting its turn or running
        # right now. One that errored or failed has already reported leaving content behind.
        waiting = Q(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES, created__lt=cutoff)
        gave_up = Q(status__in=(JobStatusChoices.STATUS_ERRORED, JobStatusChoices.STATUS_FAILED))
        # Oldest first, so a pass cut short by the timeout leaves the most stale content named.
        return ProjectStorageCleanupJob.get_jobs().filter(waiting | gave_up).order_by('created')

    def _candidates(self):
        """Group stalled cleanups by the content they name, and list those naming none."""
        grouped, unreadable = {}, []
        for job in self._stalled_cleanups():
            payload = job.data or {}
            storage_key, digest, paths = payload.get('storage_key'), payload.get('digest'), payload.get('paths')
            if not (storage_key and digest and paths):
                self.logger.warning(f'Cleanup Job {job.pk} carries no payload naming content to reclaim.')
                unreadable.append(job.pk)
                continue
            # A project cascade enqueues one cleanup per row and rows can share a tree, so
            # several Jobs reach here naming one digest. Grouping is what keeps the report
            # counting stored trees rather than Job rows.
            candidate = grouped.setdefault((storage_key, digest), {'jobs': [], 'paths': set()})
            candidate['jobs'].append(job.pk)
            candidate['paths'].update(paths)
        return grouped, unreadable

    def _classify(self, storage_key, digest, candidate, report):
        """Record one stored tree under the outcome its content is actually in."""
        entry = {'jobs': sorted(candidate['jobs']), 'storage_key': storage_key, 'digest': digest}
        try:
            # The recheck the cleanup job makes, needing the same lock for the same reason.
            with project_lock(storage_key):
                if CustomScriptProjectRevision.objects.filter(project__storage_key=storage_key, digest=digest).exists():
                    report['referenced'].append(entry)
                    return
                present = store.present_keys(config.get_storage(), storage_key, digest, candidate['paths'])
        except (OSError, StorageError) as error:
            # One unreachable project must not end the sweep.
            self.logger.warning(f'Could not inspect the content Job(s) {entry["jobs"]} name: {error}')
            report['unreadable'].extend(entry['jobs'])
            return
        if present:
            report['stranded'].append({**entry, 'paths': present})
        else:
            report['reclaimed'].append(entry)


class ProjectReconciliationJob(JobRunner):
    """
    Rebuild one project's source from its Data Source directory and drive it to a verdict.

    The directory is read when this runs rather than when it was enqueued, so two jobs queued by
    two quick synchronizations are not a correctness problem: the second stages identical content,
    resolves to the revision the first created, and enqueues no second validation.

    Per project rather than per Data Source, so one project's failure leaves its siblings to
    reconcile on their own.
    """

    class Meta:
        name = 'Custom Script Project source reconciliation'

    @classmethod
    def enqueue_reconciliation(cls, project):
        """
        Enqueue one project's reconciliation with its pk persisted on the Job row.

        The pk travels in the payload rather than as an instance link. The atomic block nests
        inside any caller transaction, so the Job and its payload commit together and the queue
        can never run a task whose payload is missing.
        """
        # In the payload: Job.clean() refuses an object type without the jobs feature.
        payload = {'project_id': project.pk}
        with transaction.atomic():
            job = cls.enqueue(**payload)
            job.data = payload
            job.save(update_fields=('data',))
        return job

    def run(self, project_id=None, **kwargs):
        """Recheck routing safety, then stage the project's current directory as a revision."""
        # Ingestion imports this module for the validation job, so the import is local.
        from . import ingestion

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = f'Refusing Custom Script Project reconciliation, because {reason}'
            self.logger.error(detail)
            raise JobFailed()
        project = CustomScriptProject.objects.filter(pk=project_id).first()
        if project is None:
            self.logger.info(f'Custom Script Project {project_id} no longer exists, nothing to reconcile.')
            return
        try:
            staged = ingestion.ingest_data_source(project)
        except (StorageError, StorageConfigurationError, OSError) as error:
            # The storage key is named deliberately, as in storage cleanup: the audience is an
            # operator working out why a synchronization produced no revision.
            detail = f'Reconciling the source of "{project}" failed and needs another run: {error}'
            self.logger.error(detail)
            raise JobFailed() from error

        revision = staged.revision
        if revision.status == RevisionStatusChoices.INVALID:
            self.logger.warning(
                f'The synchronized source cannot be stored, {len(revision.validation_errors)} problem(s) recorded.'
            )
        elif revision.status == RevisionStatusChoices.MATERIALIZED:
            self.logger.info(f'Revision {revision.digest[:12]} is staged and queued for validation.')
        elif revision.pk == project.active_revision_id:
            self.logger.info('The source has not changed, this project already serves it.')
        elif revision.status in ACTIVATABLE_REVISION_STATUSES:
            self._activate_what_the_source_matches(project, revision)
        else:
            self.logger.info(f'Revision {revision.digest[:12]} matches the source and another run owns it.')

    def _activate_what_the_source_matches(self, project, revision):
        """Promote the validated revision a reverted directory resolved to, when the policy allows."""
        # A directory reverted to a tree this project held before is content it has already
        # validated, so content addressing hands back that revision and no validation can claim it
        # again. Activation is the only step left, and without it a revert in the source would
        # silently change nothing.
        if project.activation_policy != ActivationPolicyChoices.AUTOMATIC_IF_VALID:
            self.logger.info(
                f'The source matches revision {revision.digest[:12]}, which is validated and waiting for an '
                'operator to activate it.'
            )
            return
        try:
            activation.activate_revision(revision)
        except (ActivationError, StorageError, OSError) as error:
            detail = f'The source matches revision {revision.digest[:12]}, which could not be activated: {error}'
            self.logger.error(detail)
            raise JobFailed() from error
        self.logger.info(f'Revision {revision.digest[:12]} is the active revision of its project again.')


class ProjectEntrypointRefreshJob(JobRunner):
    """
    Restage one project's stored source under its current entrypoint configuration.

    A revision freezes the project's enabled declarations into its entrypoint snapshot at
    staging time, so changing the selection has no effect until something restages. A Data
    Source-backed project gets that from a reconciliation, and an uploaded one only from here.

    The content is read when this runs rather than when it was enqueued, so two saves in
    quick succession are not a correctness problem: the second resolves to the revision the
    first created and enqueues no second validation.
    """

    class Meta:
        name = 'Custom Script Project entrypoint refresh'

    @classmethod
    def enqueue_refresh(cls, project):
        """
        Enqueue one project's entrypoint refresh with its pk persisted on the Job row.

        The pk travels in the payload rather than as an instance link. The atomic block nests
        inside any caller transaction, so the Job and its payload commit together and the queue
        can never run a task whose payload is missing.
        """
        # In the payload: Job.clean() refuses an object type without the jobs feature.
        payload = {'project_id': project.pk}
        with transaction.atomic():
            job = cls.enqueue(**payload)
            job.data = payload
            job.save(update_fields=('data',))
        return job

    def run(self, project_id=None, **kwargs):
        """Recheck routing safety, then restage the stored tree under the current selection."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = f'Refusing a Custom Script Project entrypoint refresh, because {reason}'
            self.logger.error(detail)
            raise JobFailed()
        project = CustomScriptProject.objects.filter(pk=project_id).first()
        if project is None:
            self.logger.info(f'Custom Script Project {project_id} no longer exists, nothing to refresh.')
            return
        source = project.latest_stored_revision()
        if source is None:
            # A project that has never ingested stored no content, so there is nothing to
            # restage and the selection applies to the first revision that arrives.
            self.logger.info(f'"{project}" holds no stored source yet, so its selection applies to its next revision.')
            return
        try:
            staged = service.refresh_revision_entrypoints(source)
        except (StorageError, StorageConfigurationError, OSError) as error:
            detail = f'Refreshing the entrypoints of "{project}" failed and needs another run: {error}'
            self.logger.error(detail)
            raise JobFailed() from error

        revision = staged.revision
        if revision.status == RevisionStatusChoices.MATERIALIZED:
            RevisionValidationJob.enqueue_validation(revision)
            self.logger.info(f'Revision {revision.digest[:12]} is staged and queued for validation.')
        elif revision.pk == project.active_revision_id:
            self.logger.info('The selection has not changed, this project already serves it.')
        else:
            self.logger.info(f'Revision {revision.digest[:12]} already holds a verdict for this selection.')


class RevisionValidationJob(JobRunner):
    """
    Drive one revision to a validation verdict inside a worker.

    Imports run in this process, so the worker is the isolated execution environment, and
    the rq job timeout bounds a run while the longer lease in the revision row hands the
    claim on if this worker dies without a trace. Environment
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
        the required outcome for a validation that cannot complete its last step.
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
    def enqueue_run(
        cls,
        script,
        *,
        data,
        commit,
        request=None,
        user=None,
        schedule_at=None,
        interval=None,
        notifications=None,
        event=None,
        immediate=False,
        **kwargs,
    ):
        """
        Enqueue one run of a Custom Script, immediately, at a given time, or on a recurrence.

        A one-shot run is pinned to the revision its project serves now, and the pinned identity
        is saved on the Job row inside the enqueueing transaction, so the queue can never run a
        task whose record of what it runs is missing. A recurring run is pinned to nothing and
        resolves the active revision at each occurrence. The script's own recorded metadata
        supplies the job timeout, and the notification policy unless one is given here. An
        immediate run commits its Job row before executing, so the row is visible for the whole
        run and an interrupted run leaves it behind. Raises ScriptNotExecutableError when the
        script cannot run, which covers a disabled or retired script, a disabled project, and a
        project serving no revision, ValueError for an immediate run that also asks to be
        deferred or repeated, and RuntimeError for an immediate run started inside an open
        transaction.
        """
        if immediate and (schedule_at or interval):
            raise ValueError('An immediate run cannot also be deferred or repeated.')
        branching.require_safe_routing()
        if not script.is_executable:
            raise ScriptNotExecutableError(
                f'"{script}" cannot be run right now. It is disabled, retired, or its project '
                'is disabled or is not serving a revision.'
            )
        # JobRunner.handle() re-enqueues a periodic job with the same kwargs it received, so a
        # pin carried into a recurrence would execute one frozen revision forever, long after
        # the project moved on. A recurrence therefore resolves what is active at each run.
        revision = None if interval else script.project.active_revision
        payload = {
            'revision_id': revision.pk if revision else None,
            'revision_digest': revision.digest if revision else None,
            'module_path': script.module_path,
            'class_name': script.class_name,
            'commit': bool(commit),
            'event': event,
        }
        # Input values are deliberately absent from the payload. Variables resolve to model
        # instances and uploaded files, so they are not JSON, and rendering them for the row
        # would need a policy on values an author may not want recorded.
        if script.job_timeout:
            kwargs.setdefault('job_timeout', script.job_timeout)
        kwargs.setdefault('notifications', notifications or script.notifications_default)
        if immediate:
            return cls._run_now(script, payload, data=data, request=request, user=user, **kwargs)
        with transaction.atomic():
            job = cls.enqueue(
                instance=script,
                user=user,
                data=data,
                request=request,
                schedule_at=schedule_at,
                interval=interval,
                **payload,
                **kwargs,
            )
            # A second statement because core's own row construction carries no data.
            job.data = cls._row_data(payload)
            job.save(update_fields=('data',))
        return job

    @classmethod
    def _run_now(cls, script, payload, *, data, request, user, **kwargs):
        """
        Save a pinned Job row, then run the script in this process.

        The save is durable: the row is committed and visible for the whole run, and a caller
        holding an open transaction is refused with RuntimeError. A declared job timeout is
        dropped, since no worker exists to enforce one. Re-raises anything that tears the
        process down, after recording it on the row.
        """
        # Popped here rather than left to run()'s catch-all, where the discard would be invisible.
        kwargs.pop('job_timeout', None)
        # Core's enqueue() runs the handler before returning and accepts no data, so its row and
        # this pin could only be written in two steps with the script executing between them. The
        # row is built here instead, so one INSERT carries the pin, and job_id mirrors core's own
        # immediate call. This is the plugin's one copy of a core model's construction, so it is
        # recorded as an upstream ask in docs/development/netbox-internals.md.
        object_type = ObjectType.objects.get_for_model(script, for_concrete_model=False)
        job = Job(
            object_type=object_type,
            object_id=script.pk,
            name=kwargs.pop('name', None) or cls.name,
            status=JobStatusChoices.STATUS_PENDING,
            user=user,
            job_id=uuid.uuid4(),
            queue_name=kwargs.pop('queue_name', None) or get_queue_for_model(object_type.model),
            notifications=kwargs.pop('notifications'),
            data=cls._row_data(payload),
        )
        job.full_clean()
        with transaction.atomic(durable=True):
            job.save()
        try:
            cls.handle(job_id=str(job.job_id), job=job, data=data, request=request, **payload, **kwargs)
        except BaseException:
            # handle() terminates the row itself for every Exception, so reaching here means the
            # process is going down and the row would otherwise read running for good.
            if job.status not in JobStatusChoices.TERMINAL_STATE_CHOICES:
                job.terminate(status=JobStatusChoices.STATUS_ERRORED, error='The run was interrupted.')
            raise
        return job

    @staticmethod
    def _row_data(payload):
        """Return the payload with an Event Rule's object snapshots dropped, for the Job row."""
        # payload is both the task kwargs and the row data. Only the row is readable by anyone
        # holding core.view_job, and snapshots carry the full before and after of the fired object.
        event = payload.get('event')
        if not event or 'snapshots' not in event:
            return dict(payload)
        return {**payload, 'event': {key: value for key, value in event.items() if key != 'snapshots'}}

    def run(
        self,
        *,
        revision_id=None,
        revision_digest=None,
        module_path,
        class_name,
        data,
        commit,
        request=None,
        event=None,
        **kwargs,
    ):
        """Resolve the class out of its revision and run it, recording the result."""
        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing to run a Custom Script, because {reason}')
            raise JobFailed()
        # Enabled is the administrator's field, so turning it off has to stop a run that was
        # already queued. A pinned revision is deliberately not rechecked: the point of
        # pinning is that a run executes the source it was requested against.
        script = CustomScript.objects.filter(pk=self.job.object_id).first()
        if script is not None and not (script.enabled and script.project.enabled):
            self.logger.error(f'"{script}" was disabled after this run was requested, so it was not run.')
            raise JobFailed()
        revision = self._revision_for(revision_id, script, module_path, class_name)

        storage_key = str(revision.project.storage_key)
        sanitize = build_error_sanitizer(storage_key, revision.digest)
        self.logger.info(f'Running {module_path}.{class_name} from revision {revision.digest[:12]}.')
        try:
            with revision_import_session(storage_key, revision.digest):
                try:
                    script_class = self._resolve(revision, storage_key, module_path, class_name, sanitize)
                    self._run_class(
                        script_class,
                        data=data,
                        commit=commit,
                        request=request,
                        revision=revision,
                        sanitize=sanitize,
                        event=event,
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

    def _revision_for(self, revision_id, script, module_path, class_name):
        """Return the revision this run executes, failing the Job when there is not one."""
        if revision_id is None:
            # A recurrence carries no pin, so each occurrence runs what the project serves now.
            revision = script.project.active_revision if script is not None else None
            if revision is None:
                self.logger.error(
                    f'This recurring run has no active revision to resolve, so {module_path}.{class_name} '
                    'was not run. Its project is serving nothing, or the Custom Script is gone.'
                )
                raise JobFailed()
            return revision
        revision = CustomScriptProjectRevision.objects.filter(pk=revision_id).first()
        if revision is None:
            self.logger.error(
                f'The revision this run was pinned to no longer exists, so {module_path}.{class_name} '
                'cannot be run as it was requested.'
            )
            raise JobFailed()
        return revision

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

    def _run_class(self, script_class, *, data, commit, request, revision, sanitize, event=None):
        """Run one resolved class, recording its log and output on the Job either way."""
        instance = script_class()
        instance.request = request
        instance.event = event
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


class MigrationInventoryJob(JobRunner):
    """
    Report what migrating the built-in Custom Scripts would do, changing nothing.

    The report is recorded on the Job row, so it stays readable after the run.
    """

    class Meta:
        name = 'Custom Script migration inventory'

    def run(self, **kwargs):
        """Build the report, log its headline and findings, and record it on the Job."""
        # Staging reaches ingestion, which imports this module, so the migration tier stays local.
        from .migration import dialects, plan

        report = plan.build_report()
        self.job.data = report
        counts = report['dialects']
        self.logger.info(
            f'{len(report["modules"])} module(s): {counts[dialects.NATIVE]} native, '
            f'{counts[dialects.LEGACY_IMPORT]} on legacy imports, {counts[dialects.REPORT_STYLE]} report-style.'
        )
        for finding in report['findings']:
            log = self.logger.error if finding['level'] == plan.BLOCKING else self.logger.warning
            log(finding['message'])
        self.logger.info(f'{len(report["projects"])} Custom Script Project(s) would be created.')


class MigrationStagingJob(JobRunner):
    """
    Stage the built-in Custom Scripts as Projects, leaving the built-in feature authoritative.

    Every Project takes the manual activation policy, so nothing it stages serves anything. The
    result list is recorded on the Job row.
    """

    class Meta:
        name = 'Custom Script migration staging'

    def run(self, **kwargs):
        """Refuse past the fence or on any blocking finding, then create and stage the Projects."""
        from .migration import plan, source, staging

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = f'Refusing Custom Script migration staging, because {reason}'
            self.logger.error(detail)
            raise JobFailed()

        run = MigrationRun.current()
        if run and run.state not in (MigrationStateChoices.LEGACY, MigrationStateChoices.STAGING):
            self.logger.error(
                f'Refusing to stage anything. The migration is already in the {run.state} state, and '
                'nothing may be staged once the cutover has begun.'
            )
            raise JobFailed()

        modules = source.legacy_modules()
        # The report is the gate. Its reference sweep is the inventory's business, not staging's,
        # which is why the modules are supplied rather than read a second time.
        report = plan.build_report(modules=modules)
        if report['status'] == plan.BLOCKING:
            for finding in report['findings']:
                if finding['level'] == plan.BLOCKING:
                    self.logger.error(finding['message'])
            self.logger.error('Refusing to stage anything. Resolve every blocking finding above, then run this again.')
            raise JobFailed()

        with migration_lock():
            # Nothing in the schema stops a second run, so the check and the open share a lock.
            run = MigrationRun.current() or MigrationRun.start(user=self.job.user)
        # The state moves before the work, not after, because a pass that fails partway has still
        # copied source into Projects, which is what the staging state means.
        if run.state == MigrationStateChoices.LEGACY:
            run.advance(MigrationStateChoices.STAGING)

        results = staging.stage(plan.group(modules), modules)
        self.job.data = {'projects': results}
        labels = dict(RevisionStatusChoices)
        staged = [result for result in results if not result.get('refused')]
        for result in results:
            if refusal := result.get('refused'):
                # The pass is not failed over this: the inventory is the gate on a whole run.
                self.logger.error(f'Custom Script Project {result["key"]} was not staged. {refusal}')
                continue
            status = result['revision_status']
            # Ingestion queues validation for a materialized revision and nothing else, so any
            # other status is one the revision already held when content addressing found it.
            outcome = (
                'is queued for validation'
                if status == RevisionStatusChoices.MATERIALIZED
                else f'is {labels[status]}, so no validation was queued'
            )
            self.logger.info(
                f'{"Created" if result["created"] else "Reused"} project {result["key"]}, '
                f'revision {result["revision_pk"]} {outcome}.'
            )
        pending = sum(1 for result in staged if result['revision_status'] == RevisionStatusChoices.MATERIALIZED)
        refused = len(results) - len(staged)
        self.logger.info(
            f'{len(staged)} Custom Script Project(s) staged, none activated. '
            f'{pending} awaiting a verdict, which each revision records. '
            f'{refused} refused, each named above.'
        )


class MigrationCutoverJob(JobRunner):
    """
    Cross the migration fence: capture every reference to replay, then close what a plugin can.

    Irreversible by policy rather than by mechanism. Nothing carrying history is deleted here, and
    re-running the job after a partial failure resumes rather than repeats.
    """

    class Meta:
        name = 'Custom Script migration cutover'

    def run(self, **kwargs):
        """Refuse if the state or a running job forbids it, then capture and close."""
        from .migration import cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing the Custom Script migration cutover, because {reason}')
            raise JobFailed()

        run = MigrationRun.current()
        try:
            counts = cutover.enter_cutover(run)
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        run.refresh_from_db()
        for warning in run.warnings:
            self.logger.warning(warning)
        self.logger.info(
            f'Withdrew {counts["permissions"]} permission(s) on the built-in feature, disabled '
            f'{counts["event_rules"]} Event Rule(s), cancelled {counts["schedules"]} queued job(s), '
            f'and deregistered {counts["auto_sync"]} synchronization record(s).'
        )
        self.logger.info(
            'The built-in Custom Scripts accept no further work from any user this installation '
            'grants permissions to. Activate the staged Projects next.'
        )


class MigrationActivationJob(JobRunner):
    """
    Put the staged Projects into service, so the plugin serves and its Custom Script rows exist.

    Runs after the fence and before the references move. Safe to run again: a project already
    serving its newest revision has its rows repaired.
    """

    class Meta:
        name = 'Custom Script migration activation'

    def run(self, **kwargs):
        """Activate every staged Project, reporting each outcome rather than stopping at the first."""
        from .migration import cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing Custom Script migration activation, because {reason}')
            raise JobFailed()

        run = MigrationRun.current()
        try:
            results = cutover.activate_staged(run)
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        self.job.data = {'projects': results}
        for result in results:
            self.logger.info(f'Project {result["project_key"]} {result["outcome"]}.')
        keys = [result['project_key'] for result in results]
        serving = CustomScriptProject.objects.filter(key__in=keys, active_revision__isnull=False).count()
        published = CustomScript.objects.filter(project__key__in=keys).count()
        self.logger.info(
            f'{serving} of {len(results)} Custom Script Project(s) are serving a revision, '
            f'publishing {published} Custom Script(s). Repoint the references next.'
        )


class MigrationReferencesJob(JobRunner):
    """
    Move every reference an installation holds onto the plugin's own rows.

    Runs after activation. Each part records its own completion, so a re-run continues rather
    than repeats, and a part that left work a later run could still do records none.
    """

    class Meta:
        name = 'Custom Script migration references'

    def run(self, **kwargs):
        """Repoint the Event Rules and the permissions, reporting what could not move."""
        from .migration import cutover, references

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing the Custom Script migration reference pass, because {reason}')
            raise JobFailed()

        run = MigrationRun.current()
        try:
            rules, rule_warnings = references.repoint_event_rules(run)
            permissions, permission_warnings = references.repoint_permissions(run)
            # History before schedules, so preservation happens before anything new is created, and
            # both before the built-in rows are deleted, since a Script's jobs go with it.
            history, history_warnings = references.repoint_job_history(run)
            schedules, schedule_warnings = references.recreate_schedules(run)
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        for warning in (*rule_warnings, *permission_warnings, *history_warnings, *schedule_warnings):
            self.logger.warning(warning)
        self.logger.info(
            f'Repointed {rules.get("actions", 0)} Event Rule action(s) and {rules.get("sources", 0)} '
            f'event source(s), putting {rules.get("restored", 0)} rule(s) back into service, leaving '
            f'{rules.get("unresolved", 0)} withdrawn for a later run and {rules.get("withdrawn", 0)} '
            f'withdrawn for good.'
        )
        left = permissions.get('constrained', 0) + permissions.get('unmappable', 0)
        self.logger.info(
            f'Moved {permissions.get("swapped", 0)} permission(s) onto the plugin and split '
            f'{permissions.get("split", 0)}, leaving {left} withdrawn for manual attention.'
        )
        self.logger.info(
            f'Moved {history.get("moved", 0)} Job(s) of history onto the Custom Scripts, leaving '
            f'{history.get("unresolved", 0)} unresolved script(s), {history.get("outstanding", 0)} of them '
            f'for a later run, and {history.get("modules", 0)} module Job(s) where they are.'
        )
        self.logger.info(
            f'Recreated {schedules.get("recreated", 0)} schedule(s), {schedules.get("shifted", 0)} of them '
            f'starting now rather than when they were due, and skipped {schedules.get("skipped", 0)}, '
            f'{schedules.get("outstanding", 0)} of which a later run could still recover.'
        )


class MigrationCleanupJob(JobRunner):
    """
    Delete the built-in Custom Scripts once every reference has moved onto the plugin's own rows.

    Runs last. A module something still refers to is left in place and named, and a refusal that no
    operator action would clear does not hold the run open.
    """

    class Meta:
        name = 'Custom Script migration cleanup'

    def run(self, **kwargs):
        """Refuse until the history has moved, then retire every module that carries none."""
        from .migration import cleanup, cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(f'Refusing the Custom Script migration cleanup, because {reason}')
            raise JobFailed()

        run = MigrationRun.current()
        try:
            counts, warnings = cleanup.retire_legacy(run)
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        self.job.data = counts
        for warning in warnings:
            self.logger.warning(warning)
        self.logger.info(
            f'Deleted {counts.get("modules", 0)} built-in script module(s) and the '
            f'{counts.get("scripts", 0)} Script(s) under them, along with their stored source.'
        )
        if counts.get('retained'):
            self.logger.info(
                f'{counts["retained"]} module(s) stay in place for good, because they hold history or a '
                'reference no Custom Script row can take over. Each one is named above.'
            )
        if counts.get('blocked') or counts.get('unserved'):
            self.logger.info(
                f'{counts.get("blocked", 0) + counts.get("unserved", 0)} module(s) were left for now and '
                'the migration is still open. Clear what each warning above names, then run this again.'
            )
            return
        self.logger.info(
            f'The migration is complete. {counts.get("event_rules", 0)} Event Rule(s), '
            f'{counts.get("permissions", 0)} permission(s) and {counts.get("jobs", 0)} Job(s) still '
            'name the built-in feature.'
        )


class MigrationVerificationJob(JobRunner):
    """
    Report whether a migration landed, changing nothing.

    Safe to run at any state and as often as wanted. The report is recorded on the Job row, so it
    stays readable after the run.
    """

    class Meta:
        name = 'Custom Script migration verification'

    def run(self, **kwargs):
        """Build the report, log a line per check, and record it on the Job."""
        from .migration import plan, verification

        report = verification.verify()
        self.job.data = report
        for check in report['checks']:
            log = {plan.BLOCKING: self.logger.error, plan.WARNING: self.logger.warning}.get(
                check['level'], self.logger.info
            )
            log(f'{check["name"]}: {check["message"]} (read from {check["source"]})')
        self.logger.info(
            f'{len(report["checks"])} check(s) ran and the migration reports {report["status"]}. '
            'This pass changed nothing.'
        )
