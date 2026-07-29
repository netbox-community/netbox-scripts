from django import forms
from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelBulkEditForm
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from ...models import CustomScriptModule, CustomScriptProject

__all__ = ('CustomScriptModuleBulkEditForm',)


class CustomScriptModuleBulkEditForm(PrimaryModelBulkEditForm):
    """Bulk edit form for Custom Script Modules."""

    project = DynamicModelChoiceField(
        queryset=CustomScriptProject.objects.all(),
        required=False,
        label=_('Custom Script Project'),
    )
    enabled = forms.NullBooleanField(
        required=False,
        label=_('Enabled'),
    )

    model = CustomScriptModule
    # source_path is omitted: it is unique per project, so one value across a selection collides.
    fieldsets = (FieldSet('project', 'enabled', 'description', name=_('Module')),)
    nullable_fields = ('description', 'comments')
