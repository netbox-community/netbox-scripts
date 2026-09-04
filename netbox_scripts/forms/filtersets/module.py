from django import forms
from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelFilterSetForm
from utilities.forms.constants import BOOLEAN_WITH_BLANK_CHOICES
from utilities.forms.fields import DynamicModelMultipleChoiceField, TagFilterField
from utilities.forms.rendering import FieldSet

from ...choices import ModuleDiscoveryStatusChoices
from ...models import CustomScriptModule, CustomScriptProject

__all__ = ('CustomScriptModuleFilterForm',)


class CustomScriptModuleFilterForm(PrimaryModelFilterSetForm):
    """Filter form for the Custom Script Module list view."""

    model = CustomScriptModule
    project_id = DynamicModelMultipleChoiceField(
        queryset=CustomScriptProject.objects.all(),
        required=False,
        label=_('Custom Script Project'),
    )
    source_path = forms.CharField(
        required=False,
        label=_('Source path'),
    )
    enabled = forms.NullBooleanField(
        required=False,
        widget=forms.Select(choices=BOOLEAN_WITH_BLANK_CHOICES),
        label=_('Enabled'),
    )
    discovery_status = forms.MultipleChoiceField(
        choices=ModuleDiscoveryStatusChoices,
        required=False,
        label=_('Discovery status'),
    )
    tag = TagFilterField(
        CustomScriptModule,
    )

    fieldsets = (
        FieldSet('q', 'filter_id', 'tag'),
        FieldSet(
            'project_id',
            'source_path',
            'enabled',
            'discovery_status',
            name=_('Attributes'),
        ),
    )
