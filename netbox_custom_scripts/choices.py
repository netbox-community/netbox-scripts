from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet

__all__ = (
    'ActivationPolicyChoices',
    'ProjectSourceTypeChoices',
)


class ProjectSourceTypeChoices(ChoiceSet):
    UPLOAD = 'upload'
    DATA_SOURCE = 'data_source'

    CHOICES = (
        (UPLOAD, _('Upload'), 'cyan'),
        (DATA_SOURCE, _('Data source'), 'blue'),
    )


class ActivationPolicyChoices(ChoiceSet):
    MANUAL = 'manual'
    AUTOMATIC_IF_VALID = 'automatic_if_valid'

    CHOICES = (
        (MANUAL, _('Manual'), 'orange'),
        (AUTOMATIC_IF_VALID, _('Automatic if valid'), 'green'),
    )
