import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.choices import JobNotificationChoices
from netbox.filtersets import PrimaryModelFilterSet
from utilities.filters import MultiValueCharFilter
from utilities.filtersets import register_filterset

from ..models import CustomScript, ScriptProject, ScriptProjectRevision


@register_filterset
class CustomScriptFilterSet(PrimaryModelFilterSet):
    """Filter set for the Custom Script model. metadata is deliberately unfiltered."""

    project_id = django_filters.ModelMultipleChoiceFilter(
        field_name='project',
        queryset=ScriptProject.objects.all(),
        label=_('Script Project (ID)'),
    )
    # Keyed on key rather than name, the only unique natural key on a project.
    project = django_filters.ModelMultipleChoiceFilter(
        field_name='project__key',
        queryset=ScriptProject.objects.all(),
        to_field_name='key',
        label=_('Script Project (key)'),
    )
    last_seen_revision_id = django_filters.ModelMultipleChoiceFilter(
        field_name='last_seen_revision',
        queryset=ScriptProjectRevision.objects.all(),
        label=_('Last seen revision (ID)'),
    )
    notifications_default_override = django_filters.MultipleChoiceFilter(
        choices=JobNotificationChoices,
        label=_('Notifications default override'),
    )
    # Declared explicitly because description is a TextField here, and NetBox maps only
    # CharField to a multi-value filter. Without this the one description filter in the
    # plugin that rejects repeated values would be this one.
    description = MultiValueCharFilter(
        label=_('Description'),
    )

    class Meta:
        model = CustomScript
        fields = (
            'id',
            'module_path',
            'class_name',
            'display_name',
            'enabled',
            'commit_default_override',
            'job_timeout_override',
            'notifications_default_override',
            'is_retired',
            'description',
        )

    def search(self, queryset, name, value):
        """Filter the queryset by the free-text search term."""
        return queryset.filter(
            Q(display_name__icontains=value)
            | Q(module_path__icontains=value)
            | Q(class_name__icontains=value)
            | Q(description__icontains=value)
        )
