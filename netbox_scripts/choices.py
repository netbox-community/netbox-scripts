from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet

__all__ = (
    'ActivationPolicyChoices',
    'MigrationStateChoices',
    'ModuleDiscoveryStatusChoices',
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


class ModuleDiscoveryStatusChoices(ChoiceSet):
    """Discovery outcomes for one Custom Script Module's most recent validation."""

    PENDING = 'pending'
    DISCOVERED = 'discovered'
    NO_SCRIPTS = 'no_scripts'
    FAILED = 'failed'

    CHOICES = (
        (PENDING, _('Pending'), 'gray'),
        (DISCOVERED, _('Discovered'), 'green'),
        (NO_SCRIPTS, _('No scripts'), 'yellow'),
        (FAILED, _('Failed'), 'red'),
    )


class RevisionStatusChoices(ChoiceSet):
    """
    Lifecycle states of one Script Project Revision.

    Storing a source tree and judging it fit to execute are separate steps. The storage
    layer takes a revision as far as MATERIALIZED, meaning the tree is stored and matches
    its manifest, and STAGING covers its whole write window. VALIDATING, VALID, and INVALID
    belong to project validation, which checks imports, entrypoints, and Script discovery.

    The groupings the services branch on live in constants.py, since a ChoiceSet carries
    choices and nothing else.
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


class MigrationStateChoices(ChoiceSet):
    """
    States of one migration off the built-in Custom Scripts feature.

    LEGACY and STAGING are both reversible: the built-in feature stays authoritative and the
    migration can be abandoned by deleting what staging produced. CUTOVER is the point of no
    return, and MIGRATED means the plugin serves and the built-in rows are gone. The order the
    members are declared in is the only order a run may move through.
    """

    LEGACY = 'legacy'
    STAGING = 'staging'
    CUTOVER = 'cutover'
    MIGRATED = 'migrated'

    CHOICES = (
        (LEGACY, _('Legacy'), 'gray'),
        (STAGING, _('Staging'), 'cyan'),
        (CUTOVER, _('Cutover'), 'orange'),
        (MIGRATED, _('Migrated'), 'green'),
    )
