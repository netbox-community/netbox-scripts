from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from netbox.ui import layout
from netbox.ui.panels import ContextTablePanel
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import register_model_view

from .. import activation
from ..constants import ACTIVATABLE_REVISION_STATUSES
from ..models import ScriptProject, ScriptProjectRevision
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..tables import ScriptProjectRevisionProblemTable, ScriptProjectRevisionScriptFileTable
from ..ui import ScriptProjectRevisionPanel, ScriptProjectRevisionStatePanel


def activation_message(revision, scripts):
    """Report an activation, naming what the revision publishes and what it retired."""
    if not scripts.published:
        # A revision can validate and publish nothing, which is the state an operator is most
        # likely to misread as success.
        return _('Revision {revision} is now the active revision. It publishes no Custom Scripts.').format(
            revision=revision.short_digest
        )
    published = ngettext(
        'Revision {revision} is now the active revision, publishing {count} Custom Script.',
        'Revision {revision} is now the active revision, publishing {count} Custom Scripts.',
        scripts.published,
    ).format(revision=revision.short_digest, count=scripts.published)
    if not scripts.retired:
        return published
    # A revision that drops a script retires its row, which is the outcome least likely to be
    # expected and was previously counted as though it had been published.
    retired = ngettext(
        'It retired {count} Custom Script the previous revision published.',
        'It retired {count} Custom Scripts the previous revision published.',
        scripts.retired,
    ).format(count=scripts.retired)
    return f'{published} {retired}'


@register_model_view(ScriptProjectRevision)
class ScriptProjectRevisionView(generic.ObjectView):
    """Detail view for a single revision, reached from the project's Revisions tab."""

    queryset = ScriptProjectRevision.objects.select_related('project')
    layout = layout.SimpleLayout(
        left_panels=[ScriptProjectRevisionPanel()],
        right_panels=[ScriptProjectRevisionStatePanel()],
        bottom_panels=[
            ContextTablePanel('script_files_table', title=_('Script Files')),
            ContextTablePanel('problems_table', title=_('Recorded problems')),
        ],
    )

    def get_extra_context(self, request, instance):
        """Supply the entrypoint and problem tables, withholding the problems key when there are none."""
        # The snapshot is already sorted by source path, so there is no other order to offer.
        script_files = ScriptProjectRevisionScriptFileTable(instance.script_file_snapshot, orderable=False)
        script_files.configure(request)
        # This one always renders: an empty snapshot is why a revision publishes nothing, which is
        # worth saying rather than leaving as a missing card.
        context = {'script_files_table': script_files}
        # ContextTablePanel renders nothing for an unresolved key, which is how a revision with
        # no problems avoids an empty card.
        if problems := instance.problems:
            problems_table = ScriptProjectRevisionProblemTable(problems, orderable=False)
            problems_table.configure(request)
            context['problems_table'] = problems_table
        return context


class RevisionServiceView(generic.ObjectView):
    """
    Shared plumbing for the two actions that move a project between revisions.

    GET confirms and POST performs, the shape every other state change here takes. The Revisions
    tab reaches them as ordinary links rather than posting directly, because the children view
    wraps its table in a form for bulk actions and a nested form is invalid HTML that browsers
    discard, which would submit the outer form to the tab URL instead.

    Permission is the owning project's activate permission, because what these change is what the
    project serves, rather than the revision view permission the inherited check would want.
    Reaching them still needs that view permission, since the Revisions tab is the only route to
    them. Object-level project permissions apply through the project queryset the revisions are
    filtered against.
    """

    # return_url() reaches the project after an operation that may have found it deleted, so a
    # lazy fetch here would raise past the handler that just caught it.
    queryset = ScriptProjectRevision.objects.select_related('project')

    def get_required_permission(self):
        """Require the owning project's activate permission, not the revision's own."""
        return get_permission_for_model(ScriptProject, 'activate')

    def has_permission(self):
        """Gate on the project permission, and narrow the revisions to permitted projects."""
        user = self.request.user
        if not user.has_perm(self.get_required_permission()):
            return False
        self.queryset = self.queryset.filter(project__in=ScriptProject.objects.restrict(user, 'activate'))
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


@register_model_view(ScriptProjectRevision, 'activate', path='activate')
class ScriptProjectRevisionActivateView(RevisionServiceView):
    """
    Put one specific revision of a project into service.

    The queryset excludes a revision whose status does not allow it, the active one included, so
    the route refuses what the tab declines to offer. Withholding the button is not enough: an
    action filters by permission and never by route, and reaching this view for the revision
    already in force would silently take the repair path while the confirmation page promised a
    retirement that cannot happen. Repair Scripts on the Project is the route for that.
    """

    queryset = ScriptProjectRevision.objects.select_related('project').filter(status__in=ACTIVATABLE_REVISION_STATUSES)
    template_name = 'netbox_scripts/scriptprojectrevision_activate.html'

    def post(self, request, **kwargs):
        """Activate the revision, reporting a refusal rather than raising at the user."""
        revision = self.get_object(**kwargs)
        try:
            result = activation.activate_revision(revision)
        except (ActivationError, RevisionCorruptError, StorageError, OSError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            messages.error(request, _('The revision could not be activated: {error}').format(error=error))
        else:
            messages.success(request, activation_message(revision, result.scripts))
        return redirect(self.return_url(revision))


@register_model_view(ScriptProjectRevision, 'deactivate', path='deactivate')
class ScriptProjectRevisionDeactivateView(RevisionServiceView):
    """Stand a project down from the revision it is serving."""

    template_name = 'netbox_scripts/scriptprojectrevision_deactivate.html'

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
