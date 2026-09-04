import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.filtersets import PrimaryModelFilterSet
from utilities.filtersets import register_filterset

from ..choices import FileDiscoveryStatusChoices
from ..models import ScriptFile, ScriptProject, ScriptProjectRevision


@register_filterset
class ScriptFileFilterSet(PrimaryModelFilterSet):
    """Filter set for the Script File model."""

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
    discovery_status = django_filters.MultipleChoiceFilter(
        choices=FileDiscoveryStatusChoices,
        label=_('Discovery status'),
    )
    last_discovered_revision_id = django_filters.ModelMultipleChoiceFilter(
        field_name='last_discovered_revision',
        queryset=ScriptProjectRevision.objects.all(),
        label=_('Last discovered revision (ID)'),
    )

    class Meta:
        model = ScriptFile
        fields = (
            'id',
            'source_path',
            'enabled',
            'discovery_status',
            'discovery_error',
            'description',
        )

    def search(self, queryset, name, value):
        """Filter the queryset by the free-text search term."""
        return queryset.filter(
            Q(source_path__icontains=value) | Q(description__icontains=value) | Q(project__key__icontains=value)
        )
