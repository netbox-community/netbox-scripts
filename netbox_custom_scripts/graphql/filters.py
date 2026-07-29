from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from strawberry.scalars import ID
from strawberry_django import BaseFilterLookup, FilterLookup, StrFilterLookup

from netbox.graphql.filters import PrimaryModelFilter

from ..models import CustomScriptModule, CustomScriptProject

if TYPE_CHECKING:
    from core.graphql.filters import DataSourceFilter

    from .enums import ActivationPolicyEnum, ModuleDiscoveryStatusEnum, ProjectSourceTypeEnum

__all__ = (
    'CustomScriptModuleFilter',
    'CustomScriptProjectFilter',
)


# Choice fields surface as typed enums on filter inputs only. Object types expose
# their raw values as strings, mirroring NetBox's GraphQL convention.
# storage_key is deliberately not filterable: it is internal storage/runtime
# identity, not a public lookup key.
@strawberry_django.filter_type(CustomScriptProject, lookups=True)
class CustomScriptProjectFilter(PrimaryModelFilter):
    """GraphQL filter for the Custom Script Project model."""

    name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    key: StrFilterLookup[str] | None = strawberry_django.filter_field()
    source_type: (
        BaseFilterLookup[Annotated['ProjectSourceTypeEnum', strawberry.lazy('netbox_custom_scripts.graphql.enums')]]
        | None
    ) = strawberry_django.filter_field()
    data_source: Annotated['DataSourceFilter', strawberry.lazy('core.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    data_source_id: ID | None = strawberry_django.filter_field()
    data_path: StrFilterLookup[str] | None = strawberry_django.filter_field()
    activation_policy: (
        BaseFilterLookup[Annotated['ActivationPolicyEnum', strawberry.lazy('netbox_custom_scripts.graphql.enums')]]
        | None
    ) = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()


# last_discovered_revision is not filterable here, matching the object type. REST filters it by ID.
@strawberry_django.filter_type(CustomScriptModule, lookups=True)
class CustomScriptModuleFilter(PrimaryModelFilter):
    """GraphQL filter for the Custom Script Module model."""

    project: CustomScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    source_path: StrFilterLookup[str] | None = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()
    discovery_status: (
        BaseFilterLookup[Annotated['ModuleDiscoveryStatusEnum', strawberry.lazy('netbox_custom_scripts.graphql.enums')]]
        | None
    ) = strawberry_django.filter_field()
