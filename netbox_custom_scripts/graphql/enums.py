import strawberry

from ..choices import ActivationPolicyChoices, ModuleDiscoveryStatusChoices, ProjectSourceTypeChoices

__all__ = (
    'ActivationPolicyEnum',
    'ModuleDiscoveryStatusEnum',
    'ProjectSourceTypeEnum',
)

ActivationPolicyEnum = strawberry.enum(ActivationPolicyChoices.as_enum(prefix='activation_policy'))
ModuleDiscoveryStatusEnum = strawberry.enum(ModuleDiscoveryStatusChoices.as_enum(prefix='discovery_status'))
ProjectSourceTypeEnum = strawberry.enum(ProjectSourceTypeChoices.as_enum(prefix='source_type'))
