from django.contrib import messages
from django.shortcuts import redirect
from django.utils.translation import gettext_lazy as _

from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import register_model_view

from .. import activation
from ..models import CustomScriptProject, CustomScriptProjectRevision
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError


class RevisionServiceView(generic.ObjectView):
    """
    Shared plumbing for the two buttons that move a project between revisions.

    Both are POST only. The Revisions tab lists them per row, so there is nothing for a
    confirmation page to disambiguate, and a GET goes back to the tab rather than rendering a
    template these views do not have.

    Permission is the owning project's change permission, because what these change is what the
    project serves. That needs saying, because the inherited check would otherwise restrict this
    view's own queryset by a Custom Script Project Revision permission, and a revision has no
    other surface an operator would ever have granted one for. Object-level project permissions
    still apply, through the project queryset the revisions are filtered against.
    """

    queryset = CustomScriptProjectRevision.objects.all()

    def get_required_permission(self):
        """Require the owning project's change permission, not the revision's own."""
        return get_permission_for_model(CustomScriptProject, 'change')

    def has_permission(self):
        """Gate on the project permission, and narrow the revisions to permitted projects."""
        user = self.request.user
        if not user.has_perm(self.get_required_permission()):
            return False
        self.queryset = self.queryset.filter(project__in=CustomScriptProject.objects.restrict(user, 'change'))
        return True

    @staticmethod
    def return_url(revision):
        """Return the project's Revisions tab, which is where both buttons are rendered."""
        return f'{revision.project.get_absolute_url()}revisions/'

    def get(self, request, **kwargs):
        """Nothing to confirm, so a GET just goes back to the list the button came from."""
        return redirect(self.return_url(self.get_object(**kwargs)))


@register_model_view(CustomScriptProjectRevision, 'activate', path='activate')
class CustomScriptProjectRevisionActivateView(RevisionServiceView):
    """Put one specific revision of a project into service."""

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
