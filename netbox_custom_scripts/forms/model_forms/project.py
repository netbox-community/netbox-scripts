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

__all__ = (
    'CustomScriptProjectEditForm',
    'CustomScriptProjectEntrypointsForm',
)


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


class CustomScriptProjectEntrypointsForm(PrimaryModelForm):
    """Select which of a project's source modules are its executable entrypoints."""

    entrypoints = forms.MultipleChoiceField(
        required=False,
        widget=forms.CheckboxSelectMultiple(),
        label=_('Entrypoints'),
        help_text=_('Source modules whose Custom Scripts this Project publishes. Helpers need no selection.'),
    )

    fieldsets = (FieldSet('entrypoints', name=_('Entrypoints')),)

    class Meta:
        model = CustomScriptProject
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        declared = {module.source_path: module for module in self.instance.modules.all()}
        candidates = set(self.instance.entrypoint_candidates())
        self.fields['entrypoints'].choices = [
            (path, self._label(path, declared.get(path), path in candidates))
            for path in self.instance.declarable_entrypoints()
        ]
        self.initial['entrypoints'] = [path for path, module in declared.items() if module.enabled]

    @staticmethod
    def _label(path, module, available):
        """Return the checkbox label, annotated with why an operator might care about the path."""
        if not available:
            return _('{path} (missing from the source)').format(path=path)
        if module is None:
            return path
        return _('{path} ({status})').format(path=path, status=module.get_discovery_status_display())

    def save(self, *args, **kwargs):
        """Reconcile the declarations onto the selection and return the project unchanged."""
        self.instance.select_entrypoints(self.cleaned_data['entrypoints'])
        return self.instance
