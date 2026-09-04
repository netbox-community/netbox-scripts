import strawberry

from ..choices import (
    ActivationPolicyChoices,
    ModuleDiscoveryStatusChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)

__all__ = (
    'ActivationPolicyEnum',
    'ModuleDiscoveryStatusEnum',
    'ProjectSourceTypeEnum',
    'RevisionStatusEnum',
)

ActivationPolicyEnum = strawberry.enum(
    ActivationPolicyChoices.as_enum(prefix='activation_policy'), name='NetBoxScriptActivationPolicyEnum'
)
ModuleDiscoveryStatusEnum = strawberry.enum(ModuleDiscoveryStatusChoices.as_enum(prefix='discovery_status'))
ProjectSourceTypeEnum = strawberry.enum(
    ProjectSourceTypeChoices.as_enum(prefix='source_type'), name='NetBoxScriptProjectSourceTypeEnum'
)
RevisionStatusEnum = strawberry.enum(
    RevisionStatusChoices.as_enum(prefix='status'), name='NetBoxScriptRevisionStatusEnum'
)
