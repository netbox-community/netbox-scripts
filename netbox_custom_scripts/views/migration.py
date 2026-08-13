from django.contrib import messages
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from django.views.generic import View

from core.choices import JobStatusChoices
from utilities.permissions import get_permission_for_model
from utilities.views import ContentTypePermissionRequiredMixin

from ..jobs import MigrationInventoryJob, MigrationStagingJob
from ..models import CustomScriptProject

__all__ = (
    'MigrationInventoryView',
    'MigrationStagingView',
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


def _staging_queued():
    """Report whether a staging pass is pending, scheduled or running."""
    return MigrationStagingJob.get_jobs().filter(status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES).exists()


class BaseMigrationView(ContentTypePermissionRequiredMixin, View):
    """
    Shared gate for the two migration passes.

    A pass acts on the built-in feature rather than on a model of this plugin, so it takes the
    permission for what it produces: Custom Script Projects.
    """

    def get_required_permission(self):
        return get_permission_for_model(CustomScriptProject, 'add')


class MigrationView(BaseMigrationView):
    """The page both passes are started from."""

    template_name = 'netbox_custom_scripts/migration.html'

    def get(self, request):
        """Show what each pass does, when each last ran, and where every Project it names stands."""
        inventory_job = _latest(MigrationInventoryJob)
        staging_job = _latest(MigrationStagingJob)
        return render(
            request,
            self.template_name,
            {
                'inventory_job': inventory_job,
                'staging_job': staging_job,
                'staging_queued': _staging_queued(),
                'rows': _migration_rows(request, inventory_job, staging_job),
            },
        )


class MigrationInventoryView(BaseMigrationView):
    """Queue the inventory pass and follow it to its Job."""

    def post(self, request):
        """Queue the report. It writes nothing, so it is not confirmed first."""
        job = MigrationInventoryJob.enqueue(user=request.user)
        messages.success(request, _('Queued the Custom Script migration inventory.'))
        return redirect(job.get_absolute_url())


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
        if _staging_queued():
            messages.warning(request, _('A Custom Script migration staging pass is already queued.'))
            return redirect('plugins:netbox_custom_scripts:migration')
        job = MigrationStagingJob.enqueue(user=request.user)
        messages.success(request, _('Queued Custom Script migration staging.'))
        return redirect(job.get_absolute_url())
