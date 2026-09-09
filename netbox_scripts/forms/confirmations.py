from django import forms
from django.utils.translation import gettext_lazy as _

from utilities.forms import ConfirmationForm

from ..constants import ACTIVATABLE_REVISION_STATUSES
from ..models import ScriptProjectRevision

__all__ = ('MigrationCutoverForm', 'ScriptProjectActivationForm')


class MigrationCutoverForm(ConfirmationForm):
    """Add the backup acknowledgement the cutover requires."""

    backup_taken = forms.BooleanField(
        required=True,
        label=_('The backup is taken'),
        help_text=_(
            'The database and the source storage as one restore point, taken before this pass and '
            'noted with the NetBox and plugin versions.'
        ),
    )


class ScriptProjectActivationForm(forms.Form):
    """Confirm one activatable revision of the Project being activated."""

    revision_id = forms.ModelChoiceField(
        queryset=ScriptProjectRevision.objects.none(),
        widget=forms.HiddenInput(),
    )

    def __init__(self, *args, project, **kwargs):
        super().__init__(*args, **kwargs)
        # The same eligibility activatable_revision() applies, so the route accepts only what the
        # page could have offered. An action filters by permission and never by route.
        candidates = project.revisions.filter(status__in=ACTIVATABLE_REVISION_STATUSES)
        if project.active_revision_id:
            candidates = candidates.exclude(pk=project.active_revision_id)
        self.fields['revision_id'].queryset = candidates
