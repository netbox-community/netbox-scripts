from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django

from netbox.graphql.types import ObjectType, PrimaryObjectType

from ..models import CustomScript, ScriptFile, ScriptProject, ScriptProjectRevision
from .filters import (
    CustomScriptFilter,
    ScriptFileFilter,
    ScriptProjectFilter,
    ScriptProjectRevisionFilter,
)

if TYPE_CHECKING:
    from core.graphql.types import DataSourceType


@strawberry_django.type(
    ScriptProject,
    name='NetBoxScriptProjectType',
    # Not fields='__all__': strawberry resolves the two in an if/elif, ignoring exclude.
    exclude=('active_revision',),
    filters=ScriptProjectFilter,
    pagination=True,
)
class ScriptProjectType(PrimaryObjectType):
    """GraphQL object type for the Script Project model."""

    data_source: Annotated['DataSourceType', strawberry.lazy('core.graphql.types')] | None

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        """Return the base queryset with the data source fetched in the same query."""
        return super().get_queryset(queryset, info, **kwargs).select_related('data_source')


@strawberry_django.type(
    ScriptProjectRevision,
    name='NetBoxScriptProjectRevisionType',
    # The manifest and the entrypoint snapshot are stored documents, served by the
    # diagnostics surface rather than by a general-purpose query.
    exclude=('manifest', 'entrypoint_snapshot', 'validation_job', 'validation_started'),
    filters=ScriptProjectRevisionFilter,
    pagination=True,
)
class ScriptProjectRevisionType(ObjectType):
    """GraphQL object type for the Script Project Revision model."""

    project: ScriptProjectType

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        """Return the base queryset with the project fetched in the same query."""
        return super().get_queryset(queryset, info, **kwargs).select_related('project')


@strawberry_django.type(
    ScriptFile,
    name='NetBoxScriptFileType',
    fields='__all__',
    filters=ScriptFileFilter,
    pagination=True,
)
class ScriptFileType(PrimaryObjectType):
    """GraphQL object type for the Script File model."""

    project: ScriptProjectType
    last_discovered_revision: ScriptProjectRevisionType | None

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        """Return the base queryset with the project fetched in the same query."""
        return super().get_queryset(queryset, info, **kwargs).select_related('project')


@strawberry_django.type(
    CustomScript,
    fields='__all__',
    filters=CustomScriptFilter,
    pagination=True,
)
class CustomScriptType(PrimaryObjectType):
    """GraphQL object type for the Custom Script model."""

    project: ScriptProjectType
    last_seen_revision: ScriptProjectRevisionType | None

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        """Return the base queryset with the project fetched in the same query."""
        return super().get_queryset(queryset, info, **kwargs).select_related('project')
