from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet

__all__ = (
    'ActivationPolicyChoices',
    'ProjectSourceTypeChoices',
    'RevisionStatusChoices',
)


class ProjectSourceTypeChoices(ChoiceSet):
    """Choice set for where a Custom Script Project's source tree comes from."""

    UPLOAD = 'upload'
    DATA_SOURCE = 'data_source'

    CHOICES = (
        (UPLOAD, _('Upload'), 'cyan'),
        (DATA_SOURCE, _('Data source'), 'blue'),
    )


class ActivationPolicyChoices(ChoiceSet):
    """Choice set for how a Custom Script Project activates a new revision."""

    MANUAL = 'manual'
    AUTOMATIC_IF_VALID = 'automatic_if_valid'

    CHOICES = (
        (MANUAL, _('Manual'), 'orange'),
        (AUTOMATIC_IF_VALID, _('Automatic if valid'), 'green'),
    )


class RevisionStatusChoices(ChoiceSet):
    """
    Lifecycle states of one Custom Script Project Revision.

    A revision is staged, validated, and then either activated or found invalid. Only
    the states in ACTIVATABLE may be promoted to active, so a revision that is still
    being written or that failed validation can never be served.
    """

    STAGING = 'staging'
    MATERIALIZED = 'materialized'
    STORAGE_FAILED = 'storage_failed'
    VALIDATING = 'validating'
    VALID = 'valid'
    INVALID = 'invalid'
    ACTIVE = 'active'
    RETIRED = 'retired'

    CHOICES = (
        (STAGING, _('Staging'), 'cyan'),
        (MATERIALIZED, _('Materialized'), 'purple'),
        (STORAGE_FAILED, _('Storage failed'), 'orange'),
        (VALIDATING, _('Validating'), 'blue'),
        (VALID, _('Valid'), 'green'),
        (INVALID, _('Invalid'), 'red'),
        (ACTIVE, _('Active'), 'teal'),
        (RETIRED, _('Retired'), 'gray'),
    )

    ACTIVATABLE = (VALID, RETIRED)
