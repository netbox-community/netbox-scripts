from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelForm
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from ...models import CustomScriptModule, CustomScriptProject

__all__ = ('CustomScriptModuleEditForm',)


class CustomScriptModuleEditForm(PrimaryModelForm):
    """Create and edit form for the Custom Script Module model."""

    project = DynamicModelChoiceField(
        queryset=CustomScriptProject.objects.all(),
        label=_('Custom Script Project'),
    )

    fieldsets = (FieldSet('project', 'source_path', 'enabled', 'description', 'tags', name=_('Module')),)

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
