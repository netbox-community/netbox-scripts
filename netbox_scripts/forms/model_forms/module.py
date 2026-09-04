from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelForm
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from ...models import CustomScriptModule, ScriptProject

__all__ = ('CustomScriptModuleEditForm',)


class CustomScriptModuleEditForm(PrimaryModelForm):
    """Create and edit form for the Custom Script Module model."""

    project = DynamicModelChoiceField(
        queryset=ScriptProject.objects.all(),
        label=_('Script Project'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # project and source_path are frozen after creation (model clean() enforces it), so
        # the widgets are disabled to match.
        if self.instance and self.instance.pk:
            self.fields['project'].disabled = True
            self.fields['source_path'].disabled = True

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
