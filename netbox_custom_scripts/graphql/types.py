from typing import TYPE_CHECKING, Annotated

import strawberry
import strawberry_django

from netbox.graphql.types import PrimaryObjectType

from ..models import CustomScriptProject
from .filters import CustomScriptProjectFilter

if TYPE_CHECKING:
    from core.graphql.types import DataSourceType


@strawberry_django.type(
    CustomScriptProject,
    # Not fields='__all__': strawberry resolves the two in an if/elif, ignoring exclude.
    exclude=('active_revision',),
    filters=CustomScriptProjectFilter,
    pagination=True,
)
class CustomScriptProjectType(PrimaryObjectType):
    """GraphQL object type for the Custom Script Project model."""

    data_source: Annotated['DataSourceType', strawberry.lazy('core.graphql.types')] | None

    @classmethod
    def get_queryset(cls, queryset, info, **kwargs):
        """Return the base queryset with the data source fetched in the same query."""
        return super().get_queryset(queryset, info, **kwargs).select_related('data_source')
