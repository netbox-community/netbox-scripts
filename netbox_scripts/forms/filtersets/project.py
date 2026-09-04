from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelFilterSetForm
from utilities.forms.constants import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

from ...choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ...models import ScriptProject

__all__ = ('ScriptProjectFilterForm',)


class ScriptProjectFilterForm(PrimaryModelFilterSetForm):
    """Filter form for the Script Project list view."""

    model = ScriptProject
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
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
        label=_('Enabled'),
    )
    data_source_id = DynamicModelMultipleChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_('Data source'),
    )
    tag = TagFilterField(
        ScriptProject,
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
        # A non-empty fieldsets renders only what it lists, so the inherited owner fields need
        # naming here or the sidebar hides filters the filterset and the API both support.
        FieldSet('owner_group_id', 'owner_id', name=_('Ownership')),
    )
