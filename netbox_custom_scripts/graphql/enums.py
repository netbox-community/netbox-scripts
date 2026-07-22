import strawberry

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices

__all__ = (
    'ActivationPolicyEnum',
    'ProjectSourceTypeEnum',
)

ActivationPolicyEnum = strawberry.enum(ActivationPolicyChoices.as_enum(prefix='activation_policy'))
ProjectSourceTypeEnum = strawberry.enum(ProjectSourceTypeChoices.as_enum(prefix='source_type'))
