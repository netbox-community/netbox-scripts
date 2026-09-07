import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.filtersets import ChangeLoggedModelFilterSet
from utilities.filtersets import register_filterset

from ..choices import RevisionStatusChoices
from ..models import ScriptProject, ScriptProjectRevision


@register_filterset
class ScriptProjectRevisionFilterSet(ChangeLoggedModelFilterSet):
    """Filter set for the Script Project Revision model."""

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
    status = django_filters.MultipleChoiceFilter(
        choices=RevisionStatusChoices,
        label=_('Status'),
    )

    class Meta:
        model = ScriptProjectRevision
        fields = (
            'id',
            'digest',
            'status',
            'script_file_digest',
            'file_count',
            'total_size',
            'activated',
        )

    def search(self, queryset, name, value):
        """Filter the queryset by the free-text search term."""
        return queryset.filter(Q(digest__istartswith=value) | Q(project__key__icontains=value))
