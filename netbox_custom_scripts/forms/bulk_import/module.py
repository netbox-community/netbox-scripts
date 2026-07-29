from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelImportForm
from utilities.forms.fields import CSVModelChoiceField

from ...models import CustomScriptModule, CustomScriptProject

__all__ = ('CustomScriptModuleBulkImportForm',)


class CustomScriptModuleBulkImportForm(PrimaryModelImportForm):
    """Bulk import form for Custom Script Modules."""

    # Keyed on key rather than name, the only unique natural key on a project.
    project = CSVModelChoiceField(
        queryset=CustomScriptProject.objects.all(),
        to_field_name='key',
        help_text=_('Custom Script Project (key)'),
    )

    class Meta:
        model = CustomScriptModule
        fields = (
            'project',
            'source_path',
            'enabled',
            'description',
            'owner',
            'comments',
            'tags',
        )
