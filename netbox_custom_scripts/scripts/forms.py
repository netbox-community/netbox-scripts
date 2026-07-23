from django import forms
from django.utils.translation import gettext_lazy as _

__all__ = ('ScriptForm',)


class ScriptForm(forms.Form):
    """
    Base form for executing a Custom Script.

    The script's variable fields are added dynamically by ``BaseScript.as_form()``.
    Scheduling and notification fields are not implemented yet, so the form carries only
    the commit toggle.
    """

    # Label and help text are deliberately kept identical to the wording operators
    # already know from existing script runs
    _commit = forms.BooleanField(
        required=False,
        initial=True,
        label=_('Commit changes'),
        help_text=_('Commit changes to the database (uncheck for a dry-run)'),
    )
