from django.utils.translation import gettext_lazy as _

from netbox.forms import PrimaryModelForm
from utilities.forms.rendering import FieldSet

from ...models import CustomScript

__all__ = ('CustomScriptEditForm',)


class CustomScriptEditForm(PrimaryModelForm):
    """Edit form for the administrator-owned fields of a Custom Script."""

    fieldsets = (
        FieldSet('enabled', 'tags', name=_('Custom Script')),
        FieldSet(
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            name=_('Execution overrides'),
        ),
    )

    class Meta:
        model = CustomScript
        fields = (
            'enabled',
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            'owner',
            'comments',
            'tags',
        )
