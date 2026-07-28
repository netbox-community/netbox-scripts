from django import forms
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelForm
from utilities.forms import get_field_value
from utilities.forms.fields import DynamicModelChoiceField, SlugField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import HTMXSelect

from ...choices import ProjectSourceTypeChoices
from ...models import CustomScriptProject

__all__ = ('CustomScriptProjectEditForm',)


class CustomScriptProjectEditForm(PrimaryModelForm):
    """Create and edit form for the Custom Script Project model."""

    key = SlugField(
        label=_('Key'),
        max_length=100,
        help_text=_('Stable user-facing project key. Cannot be changed after creation.'),
    )
    data_source = DynamicModelChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_('Data source'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # key and source_type are frozen after creation (model clean() enforces it), so
        # the widgets are disabled to match. Swap key off SlugWidget as well, so no
        # regenerate button renders next to a value that can no longer change.
        if self.instance and self.instance.pk:
            self.fields['key'].disabled = True
            self.fields['key'].widget = forms.TextInput()
            self.fields['source_type'].disabled = True
        # data_source and data_path apply only to data source-backed projects. The
        # htmx-refreshed selection decides, and model clean() still enforces ownership.
        if get_field_value(self, 'source_type') != ProjectSourceTypeChoices.DATA_SOURCE:
            del self.fields['data_source']
            del self.fields['data_path']

    fieldsets = (
        FieldSet('name', 'key', 'description', 'enabled', 'tags', name=_('Project')),
        FieldSet('source_type', 'data_source', 'data_path', name=_('Source')),
        FieldSet('activation_policy', name=_('Activation')),
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
        widgets = {
            'source_type': HTMXSelect(),
        }
