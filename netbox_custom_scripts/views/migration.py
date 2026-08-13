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
        """Show what each pass does and when each last ran."""
        return render(
            request,
            self.template_name,
            {
                'inventory_job': _latest(MigrationInventoryJob),
                'staging_job': _latest(MigrationStagingJob),
                'staging_queued': _staging_queued(),
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
