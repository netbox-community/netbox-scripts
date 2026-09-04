import strawberry

from ..choices import (
    ActivationPolicyChoices,
    FileDiscoveryStatusChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)

__all__ = (
    'ActivationPolicyEnum',
    'FileDiscoveryStatusEnum',
    'ProjectSourceTypeEnum',
    'RevisionStatusEnum',
)

ActivationPolicyEnum = strawberry.enum(
    ActivationPolicyChoices.as_enum(prefix='activation_policy'), name='NetBoxScriptActivationPolicyEnum'
)
FileDiscoveryStatusEnum = strawberry.enum(
    FileDiscoveryStatusChoices.as_enum(prefix='discovery_status'), name='NetBoxScriptFileDiscoveryStatusEnum'
)
ProjectSourceTypeEnum = strawberry.enum(
    ProjectSourceTypeChoices.as_enum(prefix='source_type'), name='NetBoxScriptProjectSourceTypeEnum'
)
RevisionStatusEnum = strawberry.enum(
    RevisionStatusChoices.as_enum(prefix='status'), name='NetBoxScriptRevisionStatusEnum'
)
