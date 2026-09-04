from django import forms
from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelFilterSetForm
from utilities.forms.constants import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

from ...models import CustomScript, CustomScriptProject

__all__ = ('CustomScriptFilterForm',)


class CustomScriptFilterForm(PrimaryModelFilterSetForm):
    """Filter form for the Custom Script list view."""

    model = CustomScript
    project_id = DynamicModelMultipleChoiceField(
        queryset=CustomScriptProject.objects.all(),
        required=False,
        label=_('Custom Script Project'),
    )
    module_path = forms.CharField(
        required=False,
        label=_('Module path'),
    )
    class_name = forms.CharField(
        required=False,
        label=_('Class name'),
    )
    enabled = forms.NullBooleanField(
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
        label=_('Enabled'),
    )
    is_retired = forms.NullBooleanField(
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
        label=_('Retired'),
    )
    tag = TagFilterField(
        CustomScript,
    )

    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet(
            'project_id',
            'module_path',
            'class_name',
            'enabled',
            'is_retired',
            name=_('Attributes'),
        ),
    )
