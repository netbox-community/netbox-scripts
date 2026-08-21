from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from strawberry.scalars import ID
from strawberry_django import BaseFilterLookup, FilterLookup, StrFilterLookup

from netbox.graphql.filters import ChangeLoggedModelFilter, PrimaryModelFilter

from ..models import CustomScript, CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision

if TYPE_CHECKING:
    from core.graphql.filters import DataSourceFilter

    from .enums import ActivationPolicyEnum, ModuleDiscoveryStatusEnum, ProjectSourceTypeEnum, RevisionStatusEnum

__all__ = (
    'CustomScriptFilter',
    'CustomScriptModuleFilter',
    'CustomScriptProjectFilter',
    'CustomScriptProjectRevisionFilter',
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


# The manifest and the entrypoint snapshot are not filterable: both are stored documents rather
# than lookup keys, and the diagnostics surface is where their contents belong.
@strawberry_django.filter_type(CustomScriptProjectRevision, lookups=True)
class CustomScriptProjectRevisionFilter(ChangeLoggedModelFilter):
    """GraphQL filter for the Custom Script Project Revision model."""

    project: CustomScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    digest: StrFilterLookup[str] | None = strawberry_django.filter_field()
    entrypoint_digest: StrFilterLookup[str] | None = strawberry_django.filter_field()
    status: (
        BaseFilterLookup[Annotated['RevisionStatusEnum', strawberry.lazy('netbox_custom_scripts.graphql.enums')]] | None
    ) = strawberry_django.filter_field()


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
    last_discovered_revision: CustomScriptProjectRevisionFilter | None = strawberry_django.filter_field()
    last_discovered_revision_id: ID | None = strawberry_django.filter_field()


# The three execution overrides are readable on the type but deliberately not filterable here,
# which would need a JobNotificationChoices enum core does not export.
@strawberry_django.filter_type(CustomScript, lookups=True)
class CustomScriptFilter(PrimaryModelFilter):
    """GraphQL filter for the Custom Script model."""

    project: CustomScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    module_path: StrFilterLookup[str] | None = strawberry_django.filter_field()
    class_name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    display_name: StrFilterLookup[str] | None = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()
    is_retired: FilterLookup[bool] | None = strawberry_django.filter_field()
    last_seen_revision: CustomScriptProjectRevisionFilter | None = strawberry_django.filter_field()
    last_seen_revision_id: ID | None = strawberry_django.filter_field()
