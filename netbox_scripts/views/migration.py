from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core.choices import JobStatusChoices
from netbox.ui import layout
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import ContentTypePermissionRequiredMixin, register_model_view

from ..choices import MigrationStateChoices
from ..forms import MigrationCutoverForm
from ..jobs import (
    MigrationActivationJob,
    MigrationCleanupJob,
    MigrationCutoverJob,
    MigrationInventoryJob,
    MigrationReferencesJob,
    MigrationStagingJob,
    MigrationVerificationJob,
)
from ..migration import cleanup, cutover, mapping, plan, source
from ..models import MigrationRun, ScriptProject, ScriptProjectRevision
from ..ui import MigrationRunPanel, MigrationRunVersionPanel

__all__ = (
    'MigrationActivationView',
    'MigrationCleanupView',
    'MigrationCutoverView',
    'MigrationInventoryView',
    'MigrationReferencesView',
    'MigrationRunView',
    'MigrationStagingView',
    'MigrationVerificationView',
    'MigrationView',
)


def _latest(job_class):
    """Return the most recent Job of a class, or None."""
    return job_class.get_jobs().order_by('-created').first()


def _proposed_projects(job):
    """Return the project entries one pass recorded, keyed by project key."""
    if not isinstance(getattr(job, 'data', None), dict):
        return {}
    entries = job.data.get('projects') or []
    return {entry['key']: entry for entry in entries if isinstance(entry, dict) and entry.get('key')}


def _findings(job, level, code=None):
    """Return the findings of one level that an inventory recorded, narrowed to one code if given."""
    if not isinstance(getattr(job, 'data', None), dict):
        return []
    recorded = job.data.get('findings') or []
    return [
        entry
        for entry in recorded
        if isinstance(entry, dict) and entry.get('level') == level and code in (None, entry.get('code'))
    ]


def _migration_rows(request, inventory_job, staging_job):
    """
    Return one row per Project a migration proposes or has produced, with where it stands now.

    Covers both passes, including a Project the inventory proposed that staging has not created yet.
    """
    # Neither pass records a verdict: the inventory writes nothing and staging records each
    # status before validation runs. So state is read live here rather than out of either Job.
    proposed = _proposed_projects(inventory_job)
    produced = _proposed_projects(staging_job)
    keys = list(proposed) + [key for key in produced if key not in proposed]
    projects = {
        project.key: project
        for project in ScriptProject.objects.restrict(request.user, 'view')
        .select_related('active_revision')
        .filter(key__in=keys)
    }
    newest = _newest_stored_revisions(projects.values())
    return [
        {
            'key': key,
            'name': proposed.get(key, {}).get('name') or key,
            'project': (project := projects.get(key)),
            # What the project's tree is right now: the active revision, or the newest stored
            # one for a project that has not activated anything yet.
            'revision': (project.active_revision or newest.get(project.pk)) if project is not None else None,
        }
        for key in keys
    ]


def _newest_stored_revisions(projects):
    """Return the newest revision holding content for each project, keyed by project."""
    # One query for every row. ScriptProject.current_revision answers this per instance,
    # so reading it from the template would cost one query per Project the page lists.
    newest = {}
    stored = ScriptProjectRevision.objects.filter(
        project__in=[project.pk for project in projects], digest__isnull=False
    ).order_by('project_id', '-created')
    for revision in stored:
        newest.setdefault(revision.project_id, revision)
    return newest


def _queued(job_class):
    """Report whether a pass of one class is pending, scheduled or running."""
    return job_class.get_jobs().filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()


def _completed(job):
    """Report whether a pass finished successfully."""
    return bool(job) and job.status == JobStatusChoices.STATUS_COMPLETED


class BaseMigrationView(ContentTypePermissionRequiredMixin, View):
    """Shared gate for the migration surface, and for the passes that only read or stage."""

    def get_required_permission(self):
        # A pass acts on the built-in feature, so the gate is what it produces: Projects.
        return get_permission_for_model(ScriptProject, 'add')


class DestructiveMigrationView(BaseMigrationView):
    """Gate for the steps past the fence, which close and rewrite rows of the built-in feature."""

    def get_required_permission(self):
        # Closing rows of the built-in feature is not a form of creating a Project.
        return get_permission_for_model(ScriptProject, 'migrate')


class MigrationView(BaseMigrationView):
    """The page every pass is started from."""

    template_name = 'netbox_scripts/migration.html'

    def get(self, request):
        """Show what each pass does, when each last ran, and where every Project it names stands."""
        inventory_job = _latest(MigrationInventoryJob)
        staging_job = _latest(MigrationStagingJob)
        cleanup_job = _latest(MigrationCleanupJob)
        verification_job = _latest(MigrationVerificationJob)
        run = MigrationRun.current()
        closed = MigrationRun.objects.filter(state=MigrationStateChoices.MIGRATED).order_by('-created').first()
        # Mirrors what enter_cutover accepts. Keying this off the state alone would withhold the
        # one button that finishes a crossing a crash left half done.
        crossable = bool(
            run
            and run.state in (MigrationStateChoices.STAGING, MigrationStateChoices.CUTOVER)
            and not run.step_done(cutover.STEP)
        )
        # Read only where the button would otherwise render, because this reaches the built-in rows.
        unservable = cutover.unservable_projects(run) if crossable else []
        activated = bool(run and run.step_done(cutover.ACTIVATE_STEP))
        # Gated on the frozen map rather than on the step, because that is what the predicate
        # reads and a run without one would raise here instead of refusing further along.
        not_serving = cutover.projects_not_serving(run) if mapping.recorded(run) else []
        # Staging refuses on any of these, which is otherwise invisible until its Job fails.
        blocking_findings = _findings(inventory_job, plan.BLOCKING)
        # Offered once the fence is recorded, which is the only precondition activation has.
        can_activate = bool(run and run.step_done(cutover.STEP))
        # The references name plugin rows, and only a Project in service has any.
        can_repoint = activated and not not_serving
        references_done = bool(run and cleanup.ready(run))
        # A schedule needs the built-in rows still there, so every reference step first, and the
        # pass skips a module whose Project serves nothing, so the run could not close either.
        can_clean_up = references_done and not not_serving
        # Crossing with nothing to serve is not recoverable, so the page withholds it.
        can_cut_over = crossable and not unservable
        # Read from the run, since a worker can stop before core marks the cleanup Job completed.
        migrated = run is None and closed is not None
        # Offered at every state, so a verification from before the run closed leaves this step outstanding.
        verified = migrated and _completed(verification_job) and verification_job.created > closed.completed
        # The passes in the order they run, each paired with the button's own render condition and
        # with what would make that pass complete. Two of the refusals the page reports need a
        # clause: a blocking finding withholds staging, and a Project that stopped serving reopens
        # activation, which is the remedy docs/migration.md names for it.
        sequence = (
            ('inventory', True, _completed(inventory_job)),
            # Staging creates nothing while a blocking finding stands, so it is not next.
            ('stage', not blocking_findings, _completed(staging_job)),
            # The fence completes exactly when activation becomes offerable, that being the one
            # precondition activation takes.
            ('cutover', can_cut_over, can_activate),
            ('activate', can_activate, activated and not not_serving),
            ('repoint', can_repoint, references_done),
            ('cleanup', can_clean_up, migrated),
            ('verify', migrated, verified),
        )
        return render(
            request,
            self.template_name,
            {
                'inventory_job': inventory_job,
                'staging_job': staging_job,
                'staging_queued': _queued(MigrationStagingJob),
                'blocking_findings': blocking_findings,
                # Counted rather than listed, because an installation holds hundreds of entries.
                # One entry per code, because each renders its own sentence and they say very
                # different things. A code counted nowhere here is a finding no operator ever sees.
                'warning_count': len(_findings(inventory_job, plan.WARNING, code='legacy_import')),
                'helper_count': len(_findings(inventory_job, plan.WARNING, code='publishes_nothing')),
                'branch_import_count': len(
                    _findings(inventory_job, plan.WARNING, code='import_unresolvable_in_branch')
                ),
                'undefined_script_count': len(_findings(inventory_job, plan.WARNING, code='script_not_defined_here')),
                # One finding rather than one per module, so this is presence, not a tally.
                'reports_excluded': bool(_findings(inventory_job, plan.WARNING, code='reports_excluded')),
                'rows': _migration_rows(request, inventory_job, staging_job),
                'run': run,
                'cutover_job': _latest(MigrationCutoverJob),
                'cutover_queued': _queued(MigrationCutoverJob),
                'activation_job': _latest(MigrationActivationJob),
                'activation_queued': _queued(MigrationActivationJob),
                'references_job': _latest(MigrationReferencesJob),
                'references_queued': _queued(MigrationReferencesJob),
                'cleanup_job': cleanup_job,
                'cleanup_queued': _queued(MigrationCleanupJob),
                'verification_job': verification_job,
                'can_activate': can_activate,
                'can_repoint': can_repoint,
                'can_clean_up': can_clean_up,
                'can_cut_over': can_cut_over,
                # The state moves before the closures, so both conjuncts together say the fence may
                # already have fired, which nothing else on the page distinguishes from not started.
                'cutover_interrupted': crossable and run.state == MigrationStateChoices.CUTOVER,
                'unservable_projects': unservable,
                # Only once activation has run, or this would name every Project the moment the
                # fence captured and tell the operator to redo a step they have not taken yet.
                'projects_not_serving': not_serving if activated else [],
                # The buttons carry their position, and this says which one the operator is on.
                'next_action': next((name for name, offered, complete in sequence if offered and not complete), None),
            },
        )


@register_model_view(MigrationRun)
class MigrationRunView(generic.ObjectView):
    """Detail view for one migration attempt, reached from the Migration page."""

    queryset = MigrationRun.objects.select_related('user')
    layout = layout.SimpleLayout(
        left_panels=[MigrationRunPanel()],
        right_panels=[MigrationRunVersionPanel()],
    )
    # Nothing here is editable, and a run is never cloned or deleted: the state machine is what
    # ends one. ObjectView's default trio would render three buttons that all 404.
    actions = ()

    def get_required_permission(self):
        """Require the same permission as the Migration page, not a permission of this model's own."""
        # One permission covers the whole migration surface, so the page never links a viewer
        # somewhere they cannot follow.
        return get_permission_for_model(ScriptProject, 'add')

    def has_permission(self):
        """Gate on that permission alone, without the inherited queryset restriction."""
        # ObjectPermissionRequiredMixin derives an action from whatever permission the method above
        # returns and restricts THIS view's queryset with it, which would look for an add permission
        # on this model and find none. A run is installation-global and carries nothing an object
        # constraint could select on, so the gate is the permission and there is nothing to narrow.
        return self.request.user.has_perm(self.get_required_permission())

    def get_extra_context(self, request, instance):
        """Supply the completed steps and every warning the passes recorded."""
        steps = instance.journal.get('steps', {})
        return {
            'steps': sorted(
                ({'name': name, **detail} for name, detail in steps.items()),
                key=lambda step: step.get('completed') or '',
            ),
            # The one place outstanding work is gathered: a Job log is per pass and scrolls away.
            'warnings': instance.warnings,
        }


class MigrationInventoryView(BaseMigrationView):
    """Queue the inventory pass and return to the Migration page."""

    def post(self, request):
        """Queue the report, whatever else is under way."""
        MigrationInventoryJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration inventory.'))
        # The page names the run just queued and links to it, so the Job detail is one click away
        # while the operator stays where the state table and the other pass are.
        return redirect('plugins:netbox_scripts:migration')


class MigrationStagingView(BaseMigrationView):
    """Queue the staging pass, confirming first."""

    template_name = 'netbox_scripts/migration_stage.html'

    def get(self, request):
        """Confirm, naming what staging produces and what it leaves alone."""
        return render(
            request,
            self.template_name,
            {'return_url': reverse('plugins:netbox_scripts:migration')},
        )

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Two passes at once race on project creation, where the loser gets a ValidationError
        # instead of the idempotent reuse a single pass is designed for.
        if _queued(MigrationStagingJob):
            messages.warning(request, _('A Custom Script migration staging pass is already queued.'))
            return redirect('plugins:netbox_scripts:migration')
        MigrationStagingJob.enqueue(user=request.user)
        messages.success(request, _('Queued Custom Script migration staging.'))
        return redirect('plugins:netbox_scripts:migration')


class MigrationCutoverView(DestructiveMigrationView):
    """Queue the cutover, confirming first and requiring the backup to be acknowledged."""

    template_name = 'netbox_scripts/migration_cutover.html'

    def get(self, request):
        """Confirm, saying plainly what the fence closes and that there is no way back."""
        return render(
            request,
            self.template_name,
            {
                'form': MigrationCutoverForm(),
                'report_count': source.legacy_report_count(),
                'return_url': reverse('plugins:netbox_scripts:migration'),
            },
        )

    def post(self, request):
        """Queue the cutover unless one is under way or the backup is unacknowledged."""
        if _queued(MigrationCutoverJob):
            messages.warning(request, _('A Custom Script migration cutover is already queued.'))
            return redirect('plugins:netbox_scripts:migration')
        form = MigrationCutoverForm(request.POST)
        if not form.is_valid():
            # Re-rendered rather than redirected, so the message lands beside the box to tick.
            return render(
                request,
                self.template_name,
                {
                    'form': form,
                    'report_count': source.legacy_report_count(),
                    'return_url': reverse('plugins:netbox_scripts:migration'),
                },
            )
        MigrationCutoverJob.enqueue(
            user=request.user,
            accept_concurrent_workers=form.cleaned_data['accept_concurrent_workers'],
        )
        messages.success(request, _('Queued the Custom Script migration cutover.'))
        return redirect('plugins:netbox_scripts:migration')


class MigrationActivationView(DestructiveMigrationView):
    """Queue activation of the staged Projects."""

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Not confirmed first, unlike staging and the cutover. The decision was taken at the fence,
        # and activation is a step of the cutover rather than a separate choice.
        if _queued(MigrationActivationJob):
            messages.warning(request, _('A Custom Script migration activation is already queued.'))
            return redirect('plugins:netbox_scripts:migration')
        MigrationActivationJob.enqueue(user=request.user)
        messages.success(request, _('Queued activation of the staged Script Projects.'))
        return redirect('plugins:netbox_scripts:migration')


class MigrationReferencesView(DestructiveMigrationView):
    """Queue the reference pass that moves Event Rules and permissions onto the plugin."""

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Two passes at once would both read the same journal and write the same rows.
        if _queued(MigrationReferencesJob):
            messages.warning(request, _('A Custom Script migration reference pass is already queued.'))
            return redirect('plugins:netbox_scripts:migration')
        MigrationReferencesJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration reference pass.'))
        return redirect('plugins:netbox_scripts:migration')


class MigrationCleanupView(DestructiveMigrationView):
    """Queue the cleanup, confirming first."""

    template_name = 'netbox_scripts/migration_cleanup.html'

    def get(self, request):
        """Confirm, naming what is deleted and that the stored source goes with it."""
        return render(
            request,
            self.template_name,
            {'return_url': reverse('plugins:netbox_scripts:migration')},
        )

    def post(self, request):
        """Queue the pass unless one is already under way."""
        if _queued(MigrationCleanupJob):
            messages.warning(request, _('A Custom Script migration cleanup is already queued.'))
            return redirect('plugins:netbox_scripts:migration')
        MigrationCleanupJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration cleanup.'))
        return redirect('plugins:netbox_scripts:migration')


class MigrationVerificationView(BaseMigrationView):
    """Queue the pass that reports whether a migration landed."""

    def post(self, request):
        """Queue the report, whatever else is under way."""
        MigrationVerificationJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration verification.'))
        return redirect('plugins:netbox_scripts:migration')
