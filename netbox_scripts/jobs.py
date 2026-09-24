"""Background jobs for the NetBox Scripts plugin."""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import TemporaryUploadedFile, UploadedFile
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
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
from .models import MigrationRun, NetBoxScript, ScriptProject, ScriptProjectRevision
from .models.migration import migration_lock
from .runtime.exceptions import ScriptFileImportError
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
        name = 'Script Project storage cleanup'

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
            detail = _(
                'Refusing storage cleanup and leaving source in the store, because {reason} '
                'Removing it could take away content another schema still serves: {storage_key} {digest}'
            ).format(reason=reason, storage_key=storage_key, digest=digest)
            self.logger.error(detail)
            raise JobFailed()
        # The recheck and the removal it authorizes have to be one indivisible step. A staging
        # call that creates a referencing row between them would otherwise have its content
        # deleted out from under it, which is why the project lock covers both and why staging
        # takes the same lock across its write.
        with project_lock(storage_key):
            # The digest can be re-staged under a new script file configuration between the
            # delete that recorded this job and this run. Content a current revision references
            # is left in place and the run succeeds, since there is nothing left to reclaim.
            if ScriptProjectRevision.objects.filter(project__storage_key=storage_key, digest=digest).exists():
                self.logger.info(
                    _(
                        'Leaving stored content in place, a current revision references it again: '
                        '{storage_key} {digest}'
                    ).format(storage_key=storage_key, digest=digest)
                )
                return
            try:
                storage = config.get_storage()
                store.delete_revision(storage, storage_key, digest, paths)
            except (OSError, StorageError, StorageConfigurationError) as error:
                # An unreachable backend or a missing storage entry is an expected operational
                # failure. The job log carries the detail, and the failed status plus the payload
                # persisted in data are what an operator retries from. The
                # message is rendered up front, because the job log records it verbatim rather
                # than interpolating lazy logging arguments.
                detail = _('Storage cleanup left content in the store: {storage_key} {digest}: {error}').format(
                    storage_key=storage_key, digest=digest, error=error
                )
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
        name = 'Script Project storage sweep'

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
                _(
                    'Stranded revision content, {paths_count} file(s) from cleanup Job(s) '
                    '{jobs}: {storage_key} {digest}'
                ).format(
                    paths_count=len(entry['paths']),
                    jobs=entry['jobs'],
                    storage_key=entry['storage_key'],
                    digest=entry['digest'],
                )
            )
        self.logger.info(
            _(
                '{stranded} stranded, {reclaimed} already reclaimed, '
                '{referenced} referenced again, {unreadable} unreadable.'
            ).format(
                stranded=len(report['stranded']),
                reclaimed=len(report['reclaimed']),
                referenced=len(report['referenced']),
                unreadable=len(report['unreadable']),
            )
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
                self.logger.warning(
                    _('Cleanup Job {job_pk} carries no payload naming content to reclaim.').format(job_pk=job.pk)
                )
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
                if ScriptProjectRevision.objects.filter(project__storage_key=storage_key, digest=digest).exists():
                    report['referenced'].append(entry)
                    return
                present = store.present_keys(config.get_storage(), storage_key, digest, candidate['paths'])
        except (OSError, StorageError) as error:
            # One unreachable project must not end the sweep.
            self.logger.warning(
                _('Could not inspect the content Job(s) {jobs} name: {error}').format(jobs=entry['jobs'], error=error)
            )
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
    two quick synchronizations are not a correctness problem: the second stages identical content
    and resolves to the revision the first created. Queueing is not deduplicated, so the revision
    can carry a second validation Job. The lease settles ownership, and the run that loses the
    claim ends as a failed Job rather than validating the revision twice.

    Per project rather than per Data Source, so one project's failure leaves its siblings to
    reconcile on their own.
    """

    class Meta:
        name = 'Script Project source reconciliation'

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
            detail = _('Refusing Script Project reconciliation, because {reason}').format(reason=reason)
            self.logger.error(detail)
            raise JobFailed()
        project = ScriptProject.objects.filter(pk=project_id).first()
        if project is None:
            self.logger.info(
                _('Script Project {project_id} no longer exists, nothing to reconcile.').format(project_id=project_id)
            )
            return
        try:
            staged = ingestion.ingest_data_source(project)
        except (StorageError, StorageConfigurationError, OSError) as error:
            # The storage key is named deliberately, as in storage cleanup: the audience is an
            # operator working out why a synchronization produced no revision.
            detail = _('Reconciling the source of "{project}" failed and needs another run: {error}').format(
                project=project, error=error
            )
            self.logger.error(detail)
            raise JobFailed() from error

        revision = staged.revision
        # _accept_source() moves the pointer and the intent flag with a queryset update, which
        # leaves this instance behind, and the branches below read both.
        project.refresh_from_db()
        if revision.status == RevisionStatusChoices.INVALID:
            self.logger.warning(
                _('The synchronized source cannot be stored, {count} problem(s) recorded.').format(
                    count=len(revision.validation_errors)
                )
            )
        elif revision.status == RevisionStatusChoices.MATERIALIZED:
            self.logger.info(
                _('Revision {digest} is staged and queued for validation.').format(digest=revision.digest[:12])
            )
        elif revision.pk == project.active_revision_id:
            self.logger.info(_('The source has not changed, this project already serves it.'))
        elif revision.status in ACTIVATABLE_REVISION_STATUSES:
            if project.activation_policy == ActivationPolicyChoices.MANUAL and not project.source_activation_pending:
                self.logger.info(
                    _('Revision {digest} already holds a verdict and is waiting for an operator.').format(
                        digest=revision.digest[:12]
                    )
                )
            else:
                self.logger.info(
                    _('Revision {digest} already holds a verdict and is queued for activation.').format(
                        digest=revision.digest[:12]
                    )
                )
        else:
            self.logger.info(
                _('Revision {digest} matches the source and another run owns it.').format(digest=revision.digest[:12])
            )


class ProjectScriptFileRefreshJob(JobRunner):
    """
    Restage one project's stored source under its current script file configuration.

    A revision freezes the project's enabled declarations into its script file snapshot at
    staging time, so changing the selection has no effect until something restages. A Data
    Source-backed project gets that from a reconciliation, and an uploaded one only from here.

    The content is read when this runs rather than when it was enqueued, so two saves in
    quick succession are not a correctness problem: the second resolves to the revision the
    first created. Queueing is not deduplicated, so the lease settles which run validates that
    revision, and the run that loses the claim ends as a failed Job.
    """

    class Meta:
        name = 'Script Project script file refresh'

    @classmethod
    def enqueue_refresh(cls, project):
        """
        Enqueue one project's script file refresh with its pk persisted on the Job row.

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
            detail = _('Refusing a Script Project script file refresh, because {reason}').format(reason=reason)
            self.logger.error(detail)
            raise JobFailed()
        project = ScriptProject.objects.filter(pk=project_id).first()
        if project is None:
            self.logger.info(
                _('Script Project {project_id} no longer exists, nothing to refresh.').format(project_id=project_id)
            )
            return
        from .ingestion import queue_revision_processing

        try:
            with project_lock(project.storage_key, using=require_default_database(project)):
                source = project.latest_stored_revision()
                if source is None:
                    self.logger.info(
                        _(
                            '"{project}" holds no stored source yet, so its selection applies to its next revision.'
                        ).format(project=project)
                    )
                    return
                staged = service.refresh_revision_script_files(source)
                queue_revision_processing(staged.revision)
        except (StorageError, StorageConfigurationError, OSError) as error:
            detail = _('Refreshing the script files of "{project}" failed and needs another run: {error}').format(
                project=project, error=error
            )
            self.logger.error(detail)
            raise JobFailed() from error

        revision = staged.revision
        if revision.status == RevisionStatusChoices.MATERIALIZED:
            self.logger.info(
                _('Revision {digest} is staged and queued for validation.').format(digest=revision.digest[:12])
            )
        elif revision.pk == project.active_revision_id:
            self.logger.info(_('The selection has not changed, this project already serves it.'))
        else:
            self.logger.info(
                _('Revision {digest} already holds a verdict for this selection.').format(digest=revision.digest[:12])
            )


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
        name = 'Revision validation'

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
            detail = _('Refusing revision validation, because {reason}').format(reason=reason)
            self.logger.error(detail)
            raise JobFailed()
        revision = ScriptProjectRevision.objects.filter(pk=revision_pk).first()
        if revision is None:
            self.logger.info(
                _('Revision {revision_pk} no longer exists, nothing to validate.').format(revision_pk=revision_pk)
            )
            return
        try:
            if revision.status not in (*ACTIVATABLE_REVISION_STATUSES, RevisionStatusChoices.ACTIVE):
                revision = validate_revision(revision, job=self.job, passthrough=(JobTimeoutException,))
        except ValidationStateError as error:
            self.logger.error(str(error))
            raise JobFailed() from error
        except (ScriptFileImportError, StorageError, OSError) as error:
            # An expected environment failure: the claim was rolled back, the revision is
            # claimable again, and the failed job carries the sanitized reason. The message
            # is rendered up front, because the job log records it verbatim.
            sanitize = build_error_sanitizer(str(revision.project.storage_key), revision.digest)
            detail = sanitize(
                _('Revision validation could not complete and needs another run: {error}').format(error=error)
            )
            self.logger.error(detail)
            raise JobFailed() from error
        if revision.status == RevisionStatusChoices.INVALID:
            self.logger.warning(
                _('The revision is invalid, {count} problem(s) recorded.').format(count=len(revision.validation_errors))
            )
            return
        self.logger.info(_('The revision validated as {status}.').format(status=revision.status))
        self._activate_if_requested(revision)

    def _activate_if_requested(self, revision):
        """Activate only the currently accepted source under its current policy and declaration snapshot."""
        try:
            result = activation.activate_revision(revision, automatic=True)
        except (ActivationError, StorageError, OSError) as error:
            sanitize = build_error_sanitizer(str(revision.project.storage_key), revision.digest)
            detail = sanitize(_('The revision validated but could not be activated: {error}').format(error=error))
            self.logger.error(detail)
            raise JobFailed() from error
        if result is None:
            self.logger.info(
                _('Leaving the active revision unchanged. This source is superseded or needs manual approval.')
            )
        else:
            self.logger.info(_('The revision is now the active revision of its project.'))


class NetBoxScriptJob(JobRunner):
    """
    Run one Script against the revision its enqueue pinned.

    A one-shot run's revision is fixed when the run is requested, not when the worker picks it
    up, so a queued run executes the source the operator was looking at even if the project has
    moved on since. A recurring run pins nothing and resolves the active revision at each
    occurrence. A pin is recorded on the Job row as well as passed to the worker, which is what
    makes a finished Job say what it ran rather than only what it was called.

    Everything the run needs from the tree is read through the runtime tier, so the source is
    materialized and verified against its manifest before any of it is imported, and the
    revision is unloaded afterwards. Each run therefore imports fresh and module-level state
    cannot carry from one run into the next.

    Declared pip requirements are not checked, that is a later workstream.
    """

    class Meta:
        name = 'Run Script'

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
        Enqueue one run of a Script, immediately, at a given time, or on a recurrence.

        A one-shot run is pinned to the revision its project serves now, and the pinned identity
        is saved on the Job row inside the enqueueing transaction, so the queue can never run a
        task whose record of what it runs is missing. A recurring run is pinned to nothing and
        resolves the active revision at each occurrence. The script's own recorded metadata
        supplies the job timeout, and the notification policy unless one is given here. Raises
        ValidationError for a recorded or overridden setting a run cannot be queued with, for a
        disk-backed upload, which cannot travel to a worker, and for a recurrence carrying any
        upload, each before any row is written.
        An immediate run commits its Job row before executing, so the row is
        visible for the whole run and an interrupted run leaves it behind. Raises
        ScriptNotExecutableError when the script cannot run, which covers a disabled or retired
        script, a disabled project, and a project serving no revision, ValueError for an immediate
        run that also asks to be deferred or repeated, and RuntimeError for an immediate run
        started inside an open transaction.
        """
        if immediate and (schedule_at or interval):
            raise ValueError('An immediate run cannot also be deferred or repeated.')
        branching.require_safe_routing()
        if not script.is_executable:
            raise ScriptNotExecutableError(
                _('"{script}" cannot be run right now. {reason}').format(
                    script=script, reason=script.run_refusal_reason
                )
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
        job_timeout, policy = script.run_settings(notifications=notifications)
        if job_timeout:
            kwargs.setdefault('job_timeout', job_timeout)
        kwargs.setdefault('notifications', policy)
        if immediate:
            return cls._run_now(script, payload, data=data, request=request, user=user, **kwargs)
        return cls.enqueue(
            instance=script,
            user=user,
            data=data,
            request=request,
            schedule_at=schedule_at,
            interval=interval,
            # The resolution above collapses the two cases, so this is the only record of which.
            notifications_inherited=notifications is None,
            **payload,
            **kwargs,
        )

    @classmethod
    def enqueue(cls, *args, **kwargs):
        """
        Initialize both first occurrences and core-generated recurring successors.

        Raises ValidationError when the Script has gone, when its execution settings cannot be
        queued with, when a disk-backed upload came with it, or when a recurrence carries any
        upload, which core records on the finished Job instead of rescheduling.
        """
        if kwargs.get('immediate'):
            raise ValueError('Use enqueue_run(immediate=True) for synchronous Script execution.')
        instance = kwargs.get('instance')
        script = NetBoxScript.objects.filter(pk=getattr(instance, 'pk', None)).select_related('project').first()
        if script is None:
            raise ValidationError(_('The Script no longer exists, so the run cannot be scheduled.'))
        kwargs['instance'] = script
        # Core rebuilds a successor with the row's own notifications, so setdefault cannot reach it.
        # A run queued before this flag carries none, and inherited is what it was.
        inherited = bool(kwargs.get('interval')) and kwargs.get('notifications_inherited', True)
        job_timeout, policy = script.run_settings(notifications=None if inherited else kwargs.get('notifications'))
        kwargs.setdefault('job_timeout', job_timeout)
        if inherited:
            kwargs['notifications'] = policy
        else:
            kwargs.setdefault('notifications', policy)
        if kwargs.get('interval'):
            kwargs['revision_id'] = None
            kwargs['revision_digest'] = None
        payload = {
            'revision_id': kwargs.get('revision_id'),
            'revision_digest': kwargs.get('revision_digest'),
            'module_path': kwargs.get('module_path', script.module_path),
            'class_name': kwargs.get('class_name', script.class_name),
            'commit': bool(kwargs.get('commit', script.commit_default)),
            'event': kwargs.get('event'),
        }
        kwargs.update(payload)
        # Refused before the row exists, not from the commit callback, which would leave a Job nothing will ever run.
        uploads = list((kwargs.get('data') or {}).values())
        carried = getattr(kwargs.get('request'), 'FILES', None) or {}
        if hasattr(carried, 'lists'):
            # Not values(), which reports only the last file under a repeated name.
            uploads.extend(upload for _field, group in carried.lists() for upload in group)
        else:
            # runcustomscript builds its request with a plain dict, which has no lists().
            uploads.extend(carried.values())
        # Core queues each successor with these same file objects, after an occurrence may have read or closed them.
        if kwargs.get('interval') and any(isinstance(upload, UploadedFile) for upload in uploads):
            raise ValidationError(
                _(
                    'File uploads are not supported for recurring runs, so nothing was queued. Remove the '
                    'upload or run the Script once.'
                )
            )
        # rq pickles the task and cannot pickle a disk-backed upload.
        if any(isinstance(upload, TemporaryUploadedFile) for upload in uploads):
            raise ValidationError(
                _(
                    'This request carries an upload held in a temporary file. A background Script '
                    'run supports only an upload small enough to stay in memory, so nothing was '
                    'queued.'
                )
            )
        with transaction.atomic():
            job = super().enqueue(*args, **kwargs)
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
                job.terminate(status=JobStatusChoices.STATUS_ERRORED, error=str(_('The run was interrupted.')))
            raise
        return job

    @staticmethod
    def _row_data(payload):
        """Return the payload with an Event Rule's object snapshots dropped, for the Job row."""
        # The task receives payload too, but only the row is readable by anyone holding
        # core.view_job, and snapshots carry the full before and after of the fired object.
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
            self.logger.error(_('Refusing to run a Script, because {reason}').format(reason=reason))
            raise JobFailed()
        # Enabled is the administrator's field, so turning it off has to stop a run that was
        # already queued. A pinned revision is deliberately not rechecked: the point of
        # pinning is that a run executes the source it was requested against.
        script = NetBoxScript.objects.filter(pk=self.job.object_id).first()
        if script is not None and not (script.enabled and script.project.enabled):
            self.logger.error(
                _('"{script}" was disabled after this run was requested, so it was not run.').format(script=script)
            )
            raise JobFailed()
        revision = self._revision_for(revision_id, script, module_path, class_name)

        storage_key = str(revision.project.storage_key)
        sanitize = build_error_sanitizer(storage_key, revision.digest)
        self.logger.info(
            _('Running {module_path}.{class_name} from revision {digest}.').format(
                module_path=module_path, class_name=class_name, digest=revision.digest[:12]
            )
        )
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
            detail = sanitize(_('The run could not reach the source it was pinned to: {error}').format(error=error))
            self.logger.error(detail)
            raise JobFailed() from error
        except Exception as error:
            # The run log already carries the detail, so this line only fails the Job.
            self.logger.error(sanitize(_('The Script did not finish: {error}').format(error=error)))
            raise JobFailed() from error

    def _revision_for(self, revision_id, script, module_path, class_name):
        """Return the revision this run executes, failing the Job when there is not one."""
        if revision_id is None:
            # A recurrence carries no pin, so each occurrence runs what the project serves now.
            revision = script.project.active_revision if script is not None else None
            if revision is None:
                self.logger.error(
                    _(
                        'This recurring run has no active revision to resolve, so {module_path}.{class_name} '
                        'was not run. Its project is serving nothing, or the Script is gone.'
                    ).format(module_path=module_path, class_name=class_name)
                )
                raise JobFailed()
            return revision
        revision = ScriptProjectRevision.objects.filter(pk=revision_id).first()
        if revision is None:
            self.logger.error(
                _(
                    'The revision this run was pinned to no longer exists, so {module_path}.{class_name} '
                    'cannot be run as it was requested.'
                ).format(module_path=module_path, class_name=class_name)
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
            detail = sanitize(
                _('Revision {digest} cannot supply {module_path}.{class_name}: {error}').format(
                    digest=revision.digest[:12], module_path=module_path, class_name=class_name, error=error
                )
            )
            self.logger.error(detail)
            raise JobFailed() from error

    def _run_class(self, script_class, *, data, commit, request, revision, sanitize, event=None):
        """Run one resolved class, recording its log and output on the Job either way."""
        instance = script_class()
        instance.request = request
        instance.event = event
        # Cleaned values win. A request file only fills a name the cleaned values do not carry.
        values = dict(data)
        for name, uploaded in getattr(request, 'FILES', {}).items():
            values.setdefault(name, uploaded)
        try:
            run_script(instance, data=values, commit=commit, request=request)
        finally:
            # A traceback names the file it was raised in, and that file lives in the runtime
            # cache under the storage key and digest, so the run's own identities are stripped
            # from its log and output here, before the record leaves this context.
            record = instance.get_job_data()
            sanitized = {
                'log': [{**entry, 'message': sanitize(entry.get('message'))} for entry in record.get('log', [])],
                'output': sanitize(record['output']) if isinstance(record.get('output'), str) else record.get('output'),
            }
            # The result joins the pin rather than replacing it, so a finished Job still says
            # which revision and which class it ran, not only what came out.
            self.job.data = {
                **(self.job.data or {}),
                **sanitized,
                'revision_digest': revision.digest,
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
            _(
                '{modules} module(s): {native} native, {legacy_import} on legacy imports, {report_style} report-style.'
            ).format(
                modules=len(report['modules']),
                native=counts[dialects.NATIVE],
                legacy_import=counts[dialects.LEGACY_IMPORT],
                report_style=counts[dialects.REPORT_STYLE],
            )
        )
        for finding in report['findings']:
            log = self.logger.error if finding['level'] == plan.BLOCKING else self.logger.warning
            log(finding['message'])
        self.logger.info(_('{count} Script Project(s) would be created.').format(count=len(report['projects'])))


class MigrationStagingJob(JobRunner):
    """
    Stage the built-in Custom Scripts as Projects, leaving the built-in feature authoritative.

    Every Project takes the manual activation policy, so nothing it stages serves anything. The
    result list is recorded on the Job row.
    """

    class Meta:
        name = 'Custom Script migration staging'

    @migration_lock()
    def run(self, **kwargs):
        """Refuse past the fence or on any blocking finding, then create and stage the Projects."""
        from .migration import mapping, plan, source, staging

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = _('Refusing Custom Script migration staging, because {reason}').format(reason=reason)
            self.logger.error(detail)
            raise JobFailed()

        run = MigrationRun.current()
        if run and run.state not in (MigrationStateChoices.LEGACY, MigrationStateChoices.STAGING):
            self.logger.error(
                _(
                    'Refusing to stage anything. The migration is already in the {state} state, and '
                    'nothing may be staged once the cutover has begun.'
                ).format(state=run.state)
            )
            raise JobFailed()
        # State alone would take back a run an older build left reading staging with the fence
        # already closed. A frozen map says the capture happened whatever the state says.
        if run and mapping.recorded(run) is not None:
            self.logger.error(
                _(
                    'Refusing to stage anything. This migration has already captured the built-in feature, '
                    'so the cutover has begun even though the state does not say so.'
                )
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
            self.logger.error(
                _('Refusing to stage anything. Resolve every blocking finding above, then run this again.')
            )
            raise JobFailed()

        with migration_lock():
            # The schema refuses a second open run. The lock is what lets a concurrent job find the
            # first one and reuse it instead of being refused.
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
                self.logger.error(
                    _('Script Project {key} was not staged. {refusal}').format(key=result['key'], refusal=refusal)
                )
                continue
            status = result['revision_status']
            # Any status but materialized is one the revision already held when content addressing found it.
            # Whole sentences per branch: a translator cannot reorder a substituted fragment.
            if status == RevisionStatusChoices.MATERIALIZED:
                template = (
                    _('Created project {key}, revision {revision_pk} is queued for validation.')
                    if result['created']
                    else _('Reused project {key}, revision {revision_pk} is queued for validation.')
                )
                message = template.format(key=result['key'], revision_pk=result['revision_pk'])
            else:
                template = (
                    _('Created project {key}, revision {revision_pk} is {label}.')
                    if result['created']
                    else _('Reused project {key}, revision {revision_pk} is {label}.')
                )
                message = template.format(key=result['key'], revision_pk=result['revision_pk'], label=labels[status])
            self.logger.info(message)
        pending = sum(1 for result in staged if result['revision_status'] == RevisionStatusChoices.MATERIALIZED)
        refused = len(results) - len(staged)
        self.logger.info(
            _(
                '{staged_count} Script Project(s) staged, none activated. '
                '{pending} awaiting a verdict, which each revision records. '
                '{refused} refused, each named above.'
            ).format(staged_count=len(staged), pending=pending, refused=refused)
        )


class MigrationCutoverJob(JobRunner):
    """
    Cross the migration fence: capture every reference to replay, then close what a plugin can.

    Irreversible by policy rather than by mechanism. Nothing carrying history is deleted here, and
    re-running the job after a partial failure resumes rather than repeats.
    """

    class Meta:
        name = 'Custom Script migration cutover'

    @migration_lock()
    def run(self, **kwargs):
        """Refuse if the state or a running job forbids it, then capture and close."""
        from .migration import cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(_('Refusing the Custom Script migration cutover, because {reason}').format(reason=reason))
            raise JobFailed()

        run = MigrationRun.current()
        try:
            counts = cutover.enter_cutover(run, accept_concurrent_workers=bool(kwargs.get('accept_concurrent_workers')))
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        run.refresh_from_db()
        for warning in run.warnings:
            self.logger.warning(warning)
        self.logger.info(
            _(
                'Withdrew {permissions} permission(s) on the built-in feature, disabled '
                '{event_rules} Event Rule(s), cancelled {schedules} queued job(s), closed '
                '{unreadable} whose input could not be read, and deregistered {auto_sync} '
                'synchronization record(s).'
            ).format(
                permissions=counts['permissions'],
                event_rules=counts['event_rules'],
                schedules=counts['schedules'],
                unreadable=counts['unreadable'],
                auto_sync=counts['auto_sync'],
            )
        )
        if not run.step_done(cutover.STEP):
            # Everything but the queue is closed, so the honest report is what is still open.
            self.logger.warning(
                _(
                    'The cutover is incomplete and the warnings above name what is still open. Run '
                    'this job again once they are resolved, and keep built-in Script submissions '
                    'stopped in the meantime.'
                )
            )
            return
        self.logger.info(
            _(
                'Captured permissions and Event Rules are withdrawn. Keep built-in Script submissions '
                'stopped while you activate the staged Projects and repoint references.'
            )
        )


class MigrationActivationJob(JobRunner):
    """
    Put the staged Projects into service, so the plugin serves and its Script rows exist.

    Runs after the fence and before the references move. Safe to run again: a serving project
    whose accepted source is not `valid` keeps what it serves, with its rows repaired.
    """

    class Meta:
        name = 'Custom Script migration activation'

    @migration_lock()
    def run(self, **kwargs):
        """Activate every staged Project, reporting each outcome rather than stopping at the first."""
        from .migration import cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(_('Refusing Custom Script migration activation, because {reason}').format(reason=reason))
            raise JobFailed()

        run = MigrationRun.current()
        try:
            results = cutover.activate_staged(run)
        except cutover.CutoverRefused as refusal:
            self.logger.error(str(refusal))
            raise JobFailed() from refusal

        self.job.data = {'projects': results}
        for result in results:
            self.logger.info(
                _('Project {project_key} {outcome}.').format(
                    project_key=result['project_key'], outcome=result['outcome']
                )
            )
        keys = [result['project_key'] for result in results]
        serving = ScriptProject.objects.filter(key__in=keys, active_revision__isnull=False).count()
        published = NetBoxScript.objects.filter(project__key__in=keys).count()
        self.logger.info(
            _(
                '{serving} of {total} Script Project(s) are serving a revision, '
                'publishing {published} Script(s). Repoint the references next.'
            ).format(serving=serving, total=len(results), published=published)
        )


class MigrationReferencesJob(JobRunner):
    """
    Move every reference an installation holds onto the plugin's own rows.

    Runs after activation. Each part records its own completion, so a re-run continues rather
    than repeats, and a part that left work a later run could still do records none.
    """

    class Meta:
        name = 'Custom Script migration references'

    @migration_lock()
    def run(self, **kwargs):
        """Repoint the Event Rules and the permissions, reporting what could not move."""
        from .migration import cutover, references

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(
                _('Refusing the Custom Script migration reference pass, because {reason}').format(reason=reason)
            )
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
            _(
                'Repointed {actions} Event Rule action(s) and {sources} '
                'event source(s), putting {restored} rule(s) back into service, leaving '
                '{unresolved} withdrawn for a later run and {withdrawn} '
                'withdrawn for good.'
            ).format(
                actions=rules.get('actions', 0),
                sources=rules.get('sources', 0),
                restored=rules.get('restored', 0),
                unresolved=rules.get('unresolved', 0),
                withdrawn=rules.get('withdrawn', 0),
            )
        )
        left = permissions.get('constrained', 0) + permissions.get('unmappable', 0)
        self.logger.info(
            _(
                'Moved {swapped} permission(s) onto the plugin and split '
                '{split}, leaving {left} withdrawn for manual attention.'
            ).format(swapped=permissions.get('swapped', 0), split=permissions.get('split', 0), left=left)
        )
        self.logger.info(
            _(
                'Moved {moved} Job(s) of history onto the Scripts, leaving '
                '{unresolved} unresolved script(s), {outstanding} of them '
                'for a later run, and {modules} module Job(s) where they are.'
            ).format(
                moved=history.get('moved', 0),
                unresolved=history.get('unresolved', 0),
                outstanding=history.get('outstanding', 0),
                modules=history.get('modules', 0),
            )
        )
        self.logger.info(
            _(
                'Recreated {recreated} schedule(s), {shifted} of them '
                'starting now rather than when they were due, and skipped {skipped}, '
                '{outstanding} of which a later run could still recover.'
            ).format(
                recreated=schedules.get('recreated', 0),
                shifted=schedules.get('shifted', 0),
                skipped=schedules.get('skipped', 0),
                outstanding=schedules.get('outstanding', 0),
            )
        )


class MigrationCleanupJob(JobRunner):
    """
    Delete the built-in Custom Scripts once every reference has moved onto the plugin's own rows.

    Runs last. A module something still refers to is left in place and named, and a refusal that no
    operator action would clear does not hold the run open.
    """

    class Meta:
        name = 'Custom Script migration cleanup'

    @migration_lock()
    def run(self, **kwargs):
        """Refuse until the history has moved, then retire every module that carries none."""
        from .migration import cleanup, cutover

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            self.logger.error(_('Refusing the Custom Script migration cleanup, because {reason}').format(reason=reason))
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
            _(
                'Deleted {modules} built-in script module(s) and the '
                '{scripts} Script(s) under them, along with their stored source.'
            ).format(modules=counts.get('modules', 0), scripts=counts.get('scripts', 0))
        )
        if counts.get('retained'):
            self.logger.info(
                _(
                    '{retained} module(s) stay in place for good, because they hold history or a '
                    'reference no Script row can take over. Each one is named above.'
                ).format(retained=counts['retained'])
            )
        if counts.get('blocked') or counts.get('unserved'):
            self.logger.info(
                _(
                    '{count} module(s) were left for now and '
                    'the migration is still open. Clear what each warning above names, then run this again.'
                ).format(count=counts.get('blocked', 0) + counts.get('unserved', 0))
            )
            return
        self.logger.info(
            _(
                'The migration is complete. {event_rules} Event Rule(s), '
                '{permissions} permission(s) and {jobs} Job(s) still '
                'name the built-in feature.'
            ).format(
                event_rules=counts.get('event_rules', 0),
                permissions=counts.get('permissions', 0),
                jobs=counts.get('jobs', 0),
            )
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
            log(
                _('{name}: {message} (read from {source})').format(
                    name=check['name'], message=check['message'], source=check['source']
                )
            )
        self.logger.info(
            _('{count} check(s) ran and the migration reports {status}. This pass changed nothing.').format(
                count=len(report['checks']), status=report['status']
            )
        )
