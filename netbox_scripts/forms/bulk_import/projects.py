from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelImportForm
from utilities.forms.fields import CSVChoiceField, CSVModelChoiceField

from ...choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ...models import ScriptProject
from ...permissions import ACTIVATE_PERMISSION, SOURCE_REFUSAL, moved_source_fields

__all__ = ('ScriptProjectBulkImportForm',)


class ScriptProjectBulkImportForm(PrimaryModelImportForm):
    """Bulk import form for Script Projects."""

    source_type = CSVChoiceField(
        choices=ProjectSourceTypeChoices,
        required=False,
        help_text=_('Project source type.'),
    )
    activation_policy = CSVChoiceField(
        choices=ActivationPolicyChoices,
        required=False,
        help_text=_('Revision activation policy.'),
    )
    data_source = CSVModelChoiceField(
        queryset=DataSource.objects.all(),
        to_field_name='name',
        required=False,
        help_text=_('Data source (name).'),
    )

    def clean(self):
        """Refuse a source-field move on a record that updates an existing project."""
        super().clean()
        cleaned_data = self.cleaned_data
        request = getattr(self.instance, '_request', None)
        # DictReader carries every header on every row, so presence is not change. The pk test
        # only saves a query, since moved_source_fields finds no row for an unsaved instance.
        if self.instance.pk and request and not request.user.has_perm(ACTIVATE_PERMISSION):
            for field in moved_source_fields(self.instance.pk, cleaned_data):
                self.add_error(field, SOURCE_REFUSAL)
        return cleaned_data

    class Meta:
        model = ScriptProject
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
