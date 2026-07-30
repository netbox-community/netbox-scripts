from django import forms
from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelBulkEditForm
from utilities.forms.rendering import FieldSet

from ...models import CustomScript

__all__ = ('CustomScriptBulkEditForm',)


class CustomScriptBulkEditForm(PrimaryModelBulkEditForm):
    """Bulk edit form for the administrator-owned fields of a Custom Script."""

    enabled = forms.NullBooleanField(
        required=False,
        label=_('Enabled'),
    )
    # Declaratively removes the inherited field. BulkEditView applies every non-empty field
    # with setattr, which editable=False does not stop, so leaving it would let an operator
    # overwrite a synchronization-owned column, truncated to the base field's 100 characters.
    description = None

    model = CustomScript
    fieldsets = (FieldSet('enabled', name=_('Custom Script')),)
    nullable_fields = ('comments',)
