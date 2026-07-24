from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelFilterSetForm
from utilities.forms.fields import DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

from ...choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ...models import CustomScriptProject

__all__ = ('CustomScriptProjectFilterForm',)


class CustomScriptProjectFilterForm(PrimaryModelFilterSetForm):
    """Filter form for the Custom Script Project list view."""

    model = CustomScriptProject
    name = forms.CharField(
        required=False,
        label=_('Name'),
    )
    key = forms.CharField(
        required=False,
        label=_('Key'),
    )
    source_type = forms.MultipleChoiceField(
        choices=ProjectSourceTypeChoices,
        required=False,
        label=_('Source type'),
    )
    activation_policy = forms.MultipleChoiceField(
        choices=ActivationPolicyChoices,
        required=False,
        label=_('Activation policy'),
    )
    enabled = forms.NullBooleanField(
        required=False,
        label=_('Enabled'),
    )
    data_source_id = DynamicModelMultipleChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_('Data source'),
    )
    tag = TagFilterField(
        CustomScriptProject,
    )

    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet(
            'name',
            'key',
            'source_type',
            'data_source_id',
            'activation_policy',
            'enabled',
            name=_('Attributes'),
        ),
    )
