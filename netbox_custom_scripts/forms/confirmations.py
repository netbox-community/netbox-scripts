from django import forms
from django.utils.translation import gettext_lazy as _

from utilities.forms import ConfirmationForm

__all__ = ('MigrationCutoverForm',)


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
