"""Background jobs for the Custom Scripts plugin."""

from django.db import transaction
from rq.timeouts import JobTimeoutException

from core.exceptions import JobFailed
from netbox.jobs import JobRunner

from . import activation, branching
from .choices import ActivationPolicyChoices, RevisionStatusChoices
from .constants import ACTIVATABLE_REVISION_STATUSES, VALIDATION_JOB_TIMEOUT
from .execution import ScriptNotExecutableError, run_script
from .models import CustomScript, CustomScriptProject, CustomScriptProjectRevision
from .runtime.exceptions import (
    DiscoveryError,
    EntrypointImportError,
    InvalidModulePathError,
    ScriptMetadataError,
    ScriptResolutionError,
)
from .runtime.loader import revision_import_session, unload_revision
from .runtime.resolution import resolve_script_class
from .storage import config, service, store
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


class ProjectReconciliationJob(JobRunner):
    """
    Rebuild one project's source from its Data Source directory and drive it to a verdict.

    The directory is read when this runs rather than when it was enqueued, so two jobs queued by
    two quick synchronizations are not a correctness problem: the second stages identical content,
    resolves to the revision the first created, and enqueues no second validation.

    Reconciliation is per project rather than per Data Source, because each project has its own
    lock, its own revision chain and its own activation policy, so one project's failure leaves
    its siblings to reconcile on their own.
    """

    class Meta:
        name = 'Custom Script Project source reconciliation'

    @classmethod
    def enqueue_reconciliation(cls, project):
        """
        Enqueue one project's reconciliation with its pk persisted on the Job row.

        The pk travels in the payload rather than as an instance link, because Job.clean()
        refuses an object type without the jobs feature and a project is a plain PrimaryModel.
        The atomic block nests inside any caller transaction, so the Job and its payload commit
        together and the queue handoff in Job.enqueue()'s commit hook can never run a task whose
        payload is missing.
        """
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
    Source-backed project gets that from a reconciliation, and this is the only route an
    uploaded project has, because re-uploading identical content resolves to the revision
    that already exists.

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

        The pk travels in the payload rather than as an instance link, because Job.clean()
        refuses an object type without the jobs feature and a project is a plain PrimaryModel.
        The atomic block nests inside any caller transaction, so the Job and its payload commit
        together and the queue handoff in Job.enqueue()'s commit hook can never run a task whose
        payload is missing.
        """
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
        source = project.current_revision
        if source is None or not source.digest:
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
        **kwargs,
    ):
        """
        Enqueue one run of a Custom Script, immediately, at a given time, or on a recurrence.

        A one-shot run is pinned to the revision its project serves now, and the pinned identity
        is saved on the Job row inside the enqueueing transaction, so the queue can never run a
        task whose record of what it runs is missing. A recurring run is pinned to nothing and
        resolves the active revision at each occurrence. The script's own recorded metadata
        supplies the job timeout, and the notification policy unless one is given here. Raises
        ScriptNotExecutableError when the script cannot run, which covers a disabled or retired
        script, a disabled project, and a project serving no revision.
        """
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
        }
        # Input values are deliberately absent from the payload. Variables resolve to model
        # instances and uploaded files, so they are not JSON, and rendering them for the row
        # would need a policy on values an author may not want recorded.
        if script.job_timeout:
            kwargs.setdefault('job_timeout', script.job_timeout)
        kwargs.setdefault('notifications', notifications or script.notifications_default)
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
            # An immediate run has already finished and recorded its result by the time enqueue()
            # returns, so the pin goes underneath whatever is there rather than over it.
            job.data = {**payload, **(job.data or {})}
            job.save(update_fields=('data',))
        return job

    def run(
        self, *, revision_id=None, revision_digest=None, module_path, class_name, data, commit, request=None, **kwargs
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
        """Refuse on any blocking finding, then create the proposed Projects and stage them."""
        from .migration import plan, source, staging

        # Enqueue-time safety does not carry, the job may run much later on another pod.
        if reason := branching.unsafe_routing_reason():
            detail = f'Refusing Custom Script migration staging, because {reason}'
            self.logger.error(detail)
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

        results = staging.stage(plan.group(modules), modules)
        self.job.data = {'projects': results}
        labels = dict(RevisionStatusChoices)
        for result in results:
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
        pending = sum(1 for result in results if result['revision_status'] == RevisionStatusChoices.MATERIALIZED)
        self.logger.info(
            f'{len(results)} Custom Script Project(s) staged, none activated. '
            f'{pending} awaiting a verdict, which each revision records.'
        )
