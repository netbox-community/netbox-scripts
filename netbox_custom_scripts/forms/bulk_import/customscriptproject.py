from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelImportForm
from utilities.forms.fields import CSVChoiceField, CSVModelChoiceField

from ...choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ...models import CustomScriptProject

__all__ = ('CustomScriptProjectBulkImportForm',)


class CustomScriptProjectBulkImportForm(PrimaryModelImportForm):
    """Bulk import form for Custom Script Projects."""

    source_type = CSVChoiceField(
        choices=ProjectSourceTypeChoices,
        required=False,
        help_text=_('Project source type'),
    )
    activation_policy = CSVChoiceField(
        choices=ActivationPolicyChoices,
        required=False,
        help_text=_('Revision activation policy'),
    )
    data_source = CSVModelChoiceField(
        queryset=DataSource.objects.all(),
        to_field_name='name',
        required=False,
        help_text=_('Data source (name)'),
    )

    class Meta:
        model = CustomScriptProject
        fields = (
            'name',
            'key',
            'description',
            'source_type',
            'data_source',
            'data_path',
            'activation_policy',
            'enabled',
            'owner',
            'comments',
            'tags',
        )
