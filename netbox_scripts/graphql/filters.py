from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django
from strawberry.scalars import ID
from strawberry_django import BaseFilterLookup, FilterLookup, StrFilterLookup

from netbox.graphql.filters import ChangeLoggedModelFilter, PrimaryModelFilter

from ..models import NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision

if TYPE_CHECKING:
    from core.graphql.filters import DataSourceFilter

    from .enums import ActivationPolicyEnum, FileDiscoveryStatusEnum, ProjectSourceTypeEnum, RevisionStatusEnum

__all__ = (
    'NetBoxScriptFilter',
    'ScriptFileFilter',
    'ScriptProjectFilter',
    'ScriptProjectRevisionFilter',
)


# Choice fields surface as typed enums on filter inputs only. Object types expose
# their raw values as strings, mirroring NetBox's GraphQL convention.
# storage_key is deliberately not filterable: it is internal storage/runtime
# identity, not a public lookup key.
@strawberry_django.filter_type(ScriptProject, lookups=True, name='NetBoxScriptProjectFilter')
class ScriptProjectFilter(PrimaryModelFilter):
    """GraphQL filter for the Script Project model."""

    name: StrFilterLookup | None = strawberry_django.filter_field()
    key: StrFilterLookup | None = strawberry_django.filter_field()
    source_type: (
        BaseFilterLookup[Annotated['ProjectSourceTypeEnum', strawberry.lazy('netbox_scripts.graphql.enums')]] | None
    ) = strawberry_django.filter_field()
    data_source: Annotated['DataSourceFilter', strawberry.lazy('core.graphql.filters')] | None = (
        strawberry_django.filter_field()
    )
    data_source_id: ID | None = strawberry_django.filter_field()
    data_path: StrFilterLookup | None = strawberry_django.filter_field()
    activation_policy: (
        BaseFilterLookup[Annotated['ActivationPolicyEnum', strawberry.lazy('netbox_scripts.graphql.enums')]] | None
    ) = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()


# The manifest and the entrypoint snapshot are not filterable: both are stored documents rather
# than lookup keys, and the diagnostics surface is where their contents belong.
@strawberry_django.filter_type(ScriptProjectRevision, lookups=True, name='NetBoxScriptProjectRevisionFilter')
class ScriptProjectRevisionFilter(ChangeLoggedModelFilter):
    """GraphQL filter for the Script Project Revision model."""

    project: ScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    digest: StrFilterLookup | None = strawberry_django.filter_field()
    script_file_digest: StrFilterLookup | None = strawberry_django.filter_field()
    status: (
        BaseFilterLookup[Annotated['RevisionStatusEnum', strawberry.lazy('netbox_scripts.graphql.enums')]] | None
    ) = strawberry_django.filter_field()


@strawberry_django.filter_type(ScriptFile, lookups=True, name='NetBoxScriptFileFilter')
class ScriptFileFilter(PrimaryModelFilter):
    """GraphQL filter for the Script File model."""

    project: ScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    source_path: StrFilterLookup | None = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()
    discovery_status: (
        BaseFilterLookup[Annotated['FileDiscoveryStatusEnum', strawberry.lazy('netbox_scripts.graphql.enums')]] | None
    ) = strawberry_django.filter_field()
    last_discovered_revision: ScriptProjectRevisionFilter | None = strawberry_django.filter_field()
    last_discovered_revision_id: ID | None = strawberry_django.filter_field()


# The three execution overrides are readable on the type but deliberately not filterable here,
# which would need a JobNotificationChoices enum core does not export.
@strawberry_django.filter_type(NetBoxScript, lookups=True, name='NetBoxScriptFilter')
class NetBoxScriptFilter(PrimaryModelFilter):
    """GraphQL filter for the Custom Script model."""

    project: ScriptProjectFilter | None = strawberry_django.filter_field()
    project_id: ID | None = strawberry_django.filter_field()
    module_path: StrFilterLookup | None = strawberry_django.filter_field()
    class_name: StrFilterLookup | None = strawberry_django.filter_field()
    display_name: StrFilterLookup | None = strawberry_django.filter_field()
    enabled: FilterLookup[bool] | None = strawberry_django.filter_field()
    is_retired: FilterLookup[bool] | None = strawberry_django.filter_field()
    last_seen_revision: ScriptProjectRevisionFilter | None = strawberry_django.filter_field()
    last_seen_revision_id: ID | None = strawberry_django.filter_field()
