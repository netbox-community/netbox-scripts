from django import forms
from django.utils.translation import gettext_lazy as _

from utilities.forms import ConfirmationForm

from ..constants import ACTIVATABLE_REVISION_STATUSES
from ..models import ScriptProjectRevision

__all__ = ('MigrationCutoverForm', 'ScriptProjectActivationForm')


class MigrationCutoverForm(ConfirmationForm):
    """Confirm the backup the cutover requires, and optionally accept concurrent workers."""

    backup_taken = forms.BooleanField(
        required=True,
        label=_('The backup is taken'),
        help_text=_(
            'The database and the source storage as one restore point, taken before this pass and '
            'noted with the NetBox and plugin versions.'
        ),
    )
    accept_concurrent_workers = forms.BooleanField(
        required=False,
        label=_('Accept concurrent workers'),
        help_text=_(
            'Only needed when the cutover refuses because more than one worker can take a built-in '
            'run while it works. Accepting means a run that starts in that window executes against '
            'the built-in feature and may also be recreated.'
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
