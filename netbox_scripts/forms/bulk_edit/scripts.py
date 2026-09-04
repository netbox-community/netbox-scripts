from django import forms
from django.utils.translation import gettext_lazy as _

from core.choices import JobNotificationChoices
from netbox.forms import PrimaryModelBulkEditForm
from utilities.forms.fields import ChoiceField
from utilities.forms.rendering import FieldSet
from utilities.forms.utils import add_blank_choice
from utilities.forms.widgets import BulkEditNullBooleanSelect

from ...models import NetBoxScript

__all__ = ('NetBoxScriptBulkEditForm',)


class NetBoxScriptBulkEditForm(PrimaryModelBulkEditForm):
    """Bulk edit form for the administrator-owned fields of a Custom Script."""

    enabled = forms.NullBooleanField(
        required=False,
        widget=BulkEditNullBooleanSelect(),
        label=_('Enabled'),
    )
    commit_default_override = forms.NullBooleanField(
        required=False,
        widget=BulkEditNullBooleanSelect(),
        label=_('Commit default override'),
    )
    job_timeout_override = forms.IntegerField(
        required=False,
        min_value=1,
        label=_('Job timeout override'),
    )
    notifications_default_override = ChoiceField(
        choices=add_blank_choice(JobNotificationChoices),
        required=False,
        label=_('Notifications default override'),
    )
    # Declaratively removes the inherited field. BulkEditView applies every non-empty field
    # with setattr, which editable=False does not stop, so leaving it would let an operator
    # overwrite a synchronization-owned column, truncated to the base field's 100 characters.
    description = None

    model = NetBoxScript
    fieldsets = (
        FieldSet('enabled', name=_('Custom Script')),
        FieldSet(
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            name=_('Execution overrides'),
        ),
    )
    # An empty bulk field means "leave alone", so nullable_fields is the only route back to
    # inheriting the class value.
    nullable_fields = (
        'comments',
        'commit_default_override',
        'job_timeout_override',
        'notifications_default_override',
    )
