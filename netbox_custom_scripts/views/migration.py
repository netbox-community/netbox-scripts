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
from ..jobs import (
    MigrationActivationJob,
    MigrationCleanupJob,
    MigrationCutoverJob,
    MigrationInventoryJob,
    MigrationReferencesJob,
    MigrationStagingJob,
    MigrationVerificationJob,
)
from ..migration import cleanup, cutover, plan
from ..models import CustomScriptProject, MigrationRun
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


def _findings(job, level):
    """Return the findings of one level that an inventory recorded."""
    if not isinstance(getattr(job, 'data', None), dict):
        return []
    recorded = job.data.get('findings') or []
    return [entry for entry in recorded if isinstance(entry, dict) and entry.get('level') == level]


def _migration_rows(request, inventory_job, staging_job):
    """
    Return one row per Project a migration proposes or has produced, with where it stands now.

    Rows come from both passes, so a Project the inventory proposed but staging has not created
    yet is listed as well, which is what a run of one pass without the other looks like.
    """
    # Neither pass records a verdict: the inventory writes nothing and staging records each
    # status before validation runs. So state is read live here rather than out of either Job.
    proposed = _proposed_projects(inventory_job)
    produced = _proposed_projects(staging_job)
    keys = list(proposed) + [key for key in produced if key not in proposed]
    projects = {
        project.key: project
        for project in CustomScriptProject.objects.restrict(request.user, 'view').filter(key__in=keys)
    }
    return [
        {
            'key': key,
            'name': proposed.get(key, {}).get('name') or key,
            'project': projects.get(key),
        }
        for key in keys
    ]


def _queued(job_class):
    """Report whether a pass of one class is pending, scheduled or running."""
    return job_class.get_jobs().filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()


class BaseMigrationView(ContentTypePermissionRequiredMixin, View):
    """
    Shared gate for the migration surface, and for the passes that only read or stage.

    A pass acts on the built-in feature rather than on a model of this plugin, so it takes the
    permission for what it produces: Custom Script Projects.
    """

    def get_required_permission(self):
        return get_permission_for_model(CustomScriptProject, 'add')


class DestructiveMigrationView(BaseMigrationView):
    """Gate for the steps past the fence, which close and rewrite rows of the built-in feature."""

    def get_required_permission(self):
        # Withdrawing grants, disabling rules, terminating other users' jobs and deleting rows is
        # not a form of creating a Project, which is all the staging permission authorizes.
        return get_permission_for_model(CustomScriptProject, 'migrate')


class MigrationView(BaseMigrationView):
    """The page both passes are started from."""

    template_name = 'netbox_custom_scripts/migration.html'

    def get(self, request):
        """Show what each pass does, when each last ran, and where every Project it names stands."""
        inventory_job = _latest(MigrationInventoryJob)
        staging_job = _latest(MigrationStagingJob)
        run = MigrationRun.current()
        return render(
            request,
            self.template_name,
            {
                'inventory_job': inventory_job,
                'staging_job': staging_job,
                'staging_queued': _queued(MigrationStagingJob),
                # Staging refuses on any of these, which is otherwise invisible until its Job fails.
                'blocking_findings': _findings(inventory_job, plan.BLOCKING),
                # Counted rather than listed: one entry per module, and an installation holds hundreds.
                'warning_count': len(_findings(inventory_job, plan.WARNING)),
                'rows': _migration_rows(request, inventory_job, staging_job),
                'run': run,
                'cutover_job': _latest(MigrationCutoverJob),
                'cutover_queued': _queued(MigrationCutoverJob),
                'activation_job': _latest(MigrationActivationJob),
                'activation_queued': _queued(MigrationActivationJob),
                'references_job': _latest(MigrationReferencesJob),
                'references_queued': _queued(MigrationReferencesJob),
                'cleanup_job': _latest(MigrationCleanupJob),
                'cleanup_queued': _queued(MigrationCleanupJob),
                'verification_job': _latest(MigrationVerificationJob),
                # Offered once the fence is recorded, which is the only precondition activation has.
                'can_activate': bool(run and run.step_done(cutover.STEP)),
                # The references name plugin rows, and activation is what creates them.
                'can_repoint': bool(run and run.step_done(cutover.ACTIVATE_STEP)),
                # Deleting a Script takes its Job history with it, and a schedule can only be
                # recreated while the built-in rows are still here, so every reference step first.
                'can_clean_up': bool(run and cleanup.ready(run)),
                # The fence is offered only while a run is staged and has not crossed, so the page
                # cannot invite a step the job would refuse.
                'can_cut_over': bool(run and run.state == MigrationStateChoices.STAGING),
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
        return get_permission_for_model(CustomScriptProject, 'add')

    def has_permission(self):
        """Gate on that permission alone, without the inherited queryset restriction."""
        # ObjectPermissionRequiredMixin derives an action from whatever permission the method above
        # returns and restricts THIS view's queryset with it, which would look for an add permission
        # on this model and find none. A run is installation-global and carries nothing an object
        # constraint could select on, so the gate is the permission and there is nothing to narrow.
        return self.request.user.has_perm(self.get_required_permission())

    def get_extra_context(self, request, instance):
        """Supply the completed steps, newest first, for the template's own table."""
        steps = instance.journal.get('steps', {})
        return {
            'steps': sorted(
                ({'name': name, **detail} for name, detail in steps.items()),
                key=lambda step: step.get('completed') or '',
            )
        }


class MigrationInventoryView(BaseMigrationView):
    """Queue the inventory pass and return to the Migration page."""

    def post(self, request):
        """Queue the report. It writes nothing, so it is not confirmed first."""
        MigrationInventoryJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration inventory.'))
        # The page names the run just queued and links to it, so the Job detail is one click away
        # while the operator stays where the state table and the other pass are.
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationStagingView(BaseMigrationView):
    """Queue the staging pass, confirming first because it creates Projects."""

    template_name = 'netbox_custom_scripts/migration_stage.html'

    def get(self, request):
        """Confirm, naming what staging produces and what it leaves alone."""
        return render(
            request,
            self.template_name,
            {'return_url': reverse('plugins:netbox_custom_scripts:migration')},
        )

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Two passes at once race on project creation, where the loser gets a ValidationError
        # instead of the idempotent reuse a single pass is designed for.
        if _queued(MigrationStagingJob):
            messages.warning(request, _('A Custom Script migration staging pass is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        MigrationStagingJob.enqueue(user=request.user)
        messages.success(request, _('Queued Custom Script migration staging.'))
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationCutoverView(DestructiveMigrationView):
    """Queue the cutover, confirming first because crossing the fence is a decision, not a step."""

    template_name = 'netbox_custom_scripts/migration_cutover.html'

    def get(self, request):
        """Confirm, saying plainly what the fence closes and that there is no way back."""
        return render(
            request,
            self.template_name,
            {'return_url': reverse('plugins:netbox_custom_scripts:migration')},
        )

    def post(self, request):
        """Queue the cutover unless one is already under way."""
        if _queued(MigrationCutoverJob):
            messages.warning(request, _('A Custom Script migration cutover is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        MigrationCutoverJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration cutover.'))
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationActivationView(DestructiveMigrationView):
    """Queue activation of the staged Projects."""

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Not confirmed first, unlike staging and the cutover. The decision was taken at the fence,
        # and activation is a step of the cutover rather than a separate choice.
        if _queued(MigrationActivationJob):
            messages.warning(request, _('A Custom Script migration activation is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        MigrationActivationJob.enqueue(user=request.user)
        messages.success(request, _('Queued activation of the staged Custom Script Projects.'))
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationReferencesView(DestructiveMigrationView):
    """Queue the reference pass that moves Event Rules and permissions onto the plugin."""

    def post(self, request):
        """Queue the pass unless one is already under way."""
        # Two passes at once would both read the same journal and write the same rows.
        if _queued(MigrationReferencesJob):
            messages.warning(request, _('A Custom Script migration reference pass is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        MigrationReferencesJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration reference pass.'))
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationCleanupView(DestructiveMigrationView):
    """Queue the cleanup that deletes the built-in rows, confirming first because it deletes."""

    template_name = 'netbox_custom_scripts/migration_cleanup.html'

    def get(self, request):
        """Confirm, naming what is deleted and that the stored source goes with it."""
        return render(
            request,
            self.template_name,
            {'return_url': reverse('plugins:netbox_custom_scripts:migration')},
        )

    def post(self, request):
        """Queue the pass unless one is already under way."""
        if _queued(MigrationCleanupJob):
            messages.warning(request, _('A Custom Script migration cleanup is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        MigrationCleanupJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration cleanup.'))
        return redirect('plugins:netbox_custom_scripts:migration')


class MigrationVerificationView(BaseMigrationView):
    """Queue the pass that reports whether a migration landed."""

    def post(self, request):
        """Queue the report. It writes nothing, so it is neither confirmed nor refused."""
        MigrationVerificationJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration verification.'))
        return redirect('plugins:netbox_custom_scripts:migration')
