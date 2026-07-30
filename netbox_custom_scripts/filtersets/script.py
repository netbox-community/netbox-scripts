import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.filtersets import PrimaryModelFilterSet
from utilities.filters import MultiValueCharFilter
from utilities.filtersets import register_filterset

from ..models import CustomScript, CustomScriptProject, CustomScriptProjectRevision


@register_filterset
class CustomScriptFilterSet(PrimaryModelFilterSet):
    """Filter set for the Custom Script model. metadata is deliberately unfiltered."""

    project_id = django_filters.ModelMultipleChoiceFilter(
        field_name='project',
        queryset=CustomScriptProject.objects.all(),
        label=_('Custom Script Project (ID)'),
    )
    # Keyed on key rather than name, the only unique natural key on a project.
    project = django_filters.ModelMultipleChoiceFilter(
        field_name='project__key',
        queryset=CustomScriptProject.objects.all(),
        to_field_name='key',
        label=_('Custom Script Project (key)'),
    )
    last_seen_revision_id = django_filters.ModelMultipleChoiceFilter(
        field_name='last_seen_revision',
        queryset=CustomScriptProjectRevision.objects.all(),
        label=_('Last seen revision (ID)'),
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
