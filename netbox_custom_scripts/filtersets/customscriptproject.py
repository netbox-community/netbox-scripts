import django_filters
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.filtersets import PrimaryModelFilterSet
from utilities.filtersets import register_filterset

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ..models import CustomScriptProject


@register_filterset
class CustomScriptProjectFilterSet(PrimaryModelFilterSet):
    source_type = django_filters.MultipleChoiceFilter(
        choices=ProjectSourceTypeChoices,
        label=_('Source type'),
    )
    activation_policy = django_filters.MultipleChoiceFilter(
        choices=ActivationPolicyChoices,
        label=_('Activation policy'),
    )
    data_source_id = django_filters.ModelMultipleChoiceFilter(
        field_name='data_source',
        queryset=DataSource.objects.all(),
        label=_('Data source (ID)'),
    )
    data_source = django_filters.ModelMultipleChoiceFilter(
        field_name='data_source__name',
        queryset=DataSource.objects.all(),
        to_field_name='name',
        label=_('Data source (name)'),
    )

    class Meta:
        model = CustomScriptProject
        # storage_key is internal storage/runtime identity, not a lookup key (that is
        # `key`), so neither REST nor GraphQL filters on it; it stays exposed read-only.
        fields = (
            'id',
            'name',
            'key',
            'source_type',
            'data_path',
            'activation_policy',
            'enabled',
            'description',
        )

    def search(self, queryset, name, value):
        return queryset.filter(
            Q(name__icontains=value)
            | Q(key__icontains=value)
            | Q(description__icontains=value)
            | Q(data_path__icontains=value)
        )
