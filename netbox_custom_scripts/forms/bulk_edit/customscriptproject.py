from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelBulkEditForm
from utilities.forms import add_blank_choice
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from ...choices import ActivationPolicyChoices
from ...models import CustomScriptProject

__all__ = ('CustomScriptProjectBulkEditForm',)


class CustomScriptProjectBulkEditForm(PrimaryModelBulkEditForm):
    enabled = forms.NullBooleanField(
        required=False,
        label=_('Enabled'),
    )
    activation_policy = forms.ChoiceField(
        choices=add_blank_choice(ActivationPolicyChoices),
        required=False,
        label=_('Activation policy'),
    )
    data_source = DynamicModelChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_('Data source'),
    )
    data_path = forms.CharField(
        required=False,
        label=_('Data path'),
    )

    model = CustomScriptProject
    fieldsets = (
        FieldSet('description', 'enabled', name=_('Project')),
        FieldSet('data_source', 'data_path', name=_('Source')),
        FieldSet('activation_policy', name=_('Activation')),
    )
    nullable_fields = ('description', 'comments', 'data_source', 'data_path')
