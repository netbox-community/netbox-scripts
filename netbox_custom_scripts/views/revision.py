from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _

from netbox.ui import layout
from netbox.ui.panels import ContextTablePanel
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import register_model_view

from .. import activation
from ..models import CustomScriptProject, CustomScriptProjectRevision
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..tables import CustomScriptProjectRevisionProblemTable
from ..ui import CustomScriptProjectRevisionPanel, CustomScriptProjectRevisionStatePanel


@register_model_view(CustomScriptProjectRevision)
class CustomScriptProjectRevisionView(generic.ObjectView):
    """Detail view for a single revision, reached from the project's Revisions tab."""

    queryset = CustomScriptProjectRevision.objects.select_related('project')
    layout = layout.SimpleLayout(
        left_panels=[CustomScriptProjectRevisionPanel()],
        right_panels=[CustomScriptProjectRevisionStatePanel()],
        bottom_panels=[ContextTablePanel('problems_table', title=_('Recorded problems'))],
    )

    def get_extra_context(self, request, instance):
        """Supply the problems table, withholding the key entirely when there is nothing to show."""
        # ContextTablePanel renders nothing for an unresolved key, which is how a revision with
        # no problems avoids an empty card.
        if not (problems := instance.problems):
            return {}
        table = CustomScriptProjectRevisionProblemTable(problems, orderable=False)
        table.configure(request)
        return {'problems_table': table}


class RevisionServiceView(generic.ObjectView):
    """
    Shared plumbing for the two actions that move a project between revisions.

    GET confirms and POST performs, the shape every other state change here takes. The Revisions
    tab reaches them as ordinary links rather than posting directly, because the children view
    wraps its table in a form for bulk actions and a nested form is invalid HTML that browsers
    discard, which would submit the outer form to the tab URL instead.

    Permission is the owning project's activate permission, because what these change is what the
    project serves. The inherited check would otherwise want a revision permission, and a revision
    has no other surface anyone would grant one for. Object-level project permissions still apply,
    through the project queryset the revisions are filtered against.
    """

    queryset = CustomScriptProjectRevision.objects.all()

    def get_required_permission(self):
        """Require the owning project's activate permission, not the revision's own."""
        return get_permission_for_model(CustomScriptProject, 'activate')

    def has_permission(self):
        """Gate on the project permission, and narrow the revisions to permitted projects."""
        user = self.request.user
        if not user.has_perm(self.get_required_permission()):
            return False
        self.queryset = self.queryset.filter(project__in=CustomScriptProject.objects.restrict(user, 'activate'))
        return True

    @staticmethod
    def return_url(revision):
        """Return the project's Revisions tab, which is where both buttons are rendered."""
        return f'{revision.project.get_absolute_url()}revisions/'

    def get(self, request, **kwargs):
        """Render the confirmation, which posts back to this same route."""
        revision = self.get_object(**kwargs)
        return render(
            request,
            self.template_name,
            {
                'object': revision,
                'project': revision.project,
                'return_url': self.return_url(revision),
            },
        )


@register_model_view(CustomScriptProjectRevision, 'activate', path='activate')
class CustomScriptProjectRevisionActivateView(RevisionServiceView):
    """Put one specific revision of a project into service."""

    template_name = 'netbox_custom_scripts/customscriptprojectrevision_activate.html'

    def post(self, request, **kwargs):
        """Activate the revision, reporting a refusal rather than raising at the user."""
        revision = self.get_object(**kwargs)
        try:
            activation.activate_revision(revision)
        except (ActivationError, RevisionCorruptError, StorageError, OSError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            messages.error(request, _('The revision could not be activated: {error}').format(error=error))
        else:
            messages.success(
                request,
                _('Revision {revision} is now the active revision.').format(revision=revision.short_digest),
            )
        return redirect(self.return_url(revision))


@register_model_view(CustomScriptProjectRevision, 'deactivate', path='deactivate')
class CustomScriptProjectRevisionDeactivateView(RevisionServiceView):
    """Stand a project down from the revision it is serving."""

    template_name = 'netbox_custom_scripts/customscriptprojectrevision_deactivate.html'

    def post(self, request, **kwargs):
        """Retire the revision and clear the project's pointer, reporting a refusal."""
        revision = self.get_object(**kwargs)
        try:
            activation.deactivate_revision(revision)
        except (ActivationError, StorageError) as error:
            messages.error(request, _('The revision could not be deactivated: {error}').format(error=error))
        else:
            messages.success(
                request,
                _('Revision {revision} is retired and the Project is serving nothing.').format(
                    revision=revision.short_digest
                ),
            )
        return redirect(self.return_url(revision))
