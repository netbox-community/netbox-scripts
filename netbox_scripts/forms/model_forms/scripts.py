from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelForm
from utilities.forms.fields import DynamicModelChoiceField
from utilities.forms.rendering import FieldSet

from ...models import NetBoxScript, ScriptFile, ScriptProject

__all__ = (
    'NetBoxScriptEditForm',
    'ScriptFileEditForm',
)


class NetBoxScriptEditForm(PrimaryModelForm):
    """Edit form for the administrator-owned fields of a Script."""

    fieldsets = (
        FieldSet('enabled', 'tags', name=_('Script')),
        FieldSet(
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            name=_('Execution overrides'),
        ),
    )

    class Meta:
        model = NetBoxScript
        fields = (
            'enabled',
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            'owner',
            'comments',
            'tags',
        )


class ScriptFileEditForm(PrimaryModelForm):
    """Edit form for the Script File model. A declaration is created on its Project."""

    project = DynamicModelChoiceField(
        queryset=ScriptProject.objects.all(),
        label=_('Script Project'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Frozen once the row exists (model clean() enforces it), so the widgets match. Only
        # edit reaches this form, so the guard is for an unbound instance a test builds.
        if self.instance and self.instance.pk:
            self.fields['project'].disabled = True
            self.fields['source_path'].disabled = True

    fieldsets = (FieldSet('project', 'source_path', 'enabled', 'description', 'tags', name=_('Script File')),)

    class Meta:
        model = ScriptFile
        fields = (
            'project',
            'source_path',
            'enabled',
            'description',
            'owner',
            'comments',
            'tags',
        )
