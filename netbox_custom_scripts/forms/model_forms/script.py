from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelForm
from utilities.forms.rendering import FieldSet

from ...models import CustomScript

__all__ = ('CustomScriptEditForm',)

# Concrete columns this form may write, plus the change-tracking column. owner_group is
# absent because the model has only the owner FK, the group narrows the owner choices.
_WRITABLE_COLUMNS = ('enabled', 'comments', 'owner', 'custom_field_data', 'last_updated')


class CustomScriptEditForm(PrimaryModelForm):
    """
    Edit form for the administrator-owned fields of a Custom Script.

    Saves only the columns it renders, so a save concurrent with an activation cannot
    revert the derived fields synchronization owns.
    """

    fieldsets = (FieldSet('enabled', 'tags', name=_('Custom Script')),)

    class Meta:
        model = CustomScript
        fields = (
            'enabled',
            'owner',
            'comments',
            'tags',
        )

    def save(self, commit=True):
        """Persist only the rendered columns. Returns the instance, unsaved when commit is false."""
        if not commit:
            return super().save(commit=False)
        instance = super().save(commit=False)
        instance.save(update_fields=_WRITABLE_COLUMNS)
        self.save_m2m()
        return instance
