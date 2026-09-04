import logging

from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet

__all__ = ('LogLevelChoices',)


class LogLevelChoices(ChoiceSet):
    """
    Message severity levels for Custom Script logging.

    Each level maps to a stdlib logging level through SYSTEM_LEVELS.
    """

    LOG_DEBUG = 'debug'
    LOG_INFO = 'info'
    LOG_SUCCESS = 'success'
    LOG_WARNING = 'warning'
    LOG_FAILURE = 'failure'

    CHOICES = (
        (LOG_DEBUG, _('Debug'), 'teal'),
        (LOG_INFO, _('Info'), 'cyan'),
        (LOG_SUCCESS, _('Success'), 'green'),
        (LOG_WARNING, _('Warning'), 'yellow'),
        (LOG_FAILURE, _('Failure'), 'red'),
    )

    # Forwarding map to stdlib logging levels. The stdlib has no SUCCESS level, so
    # LOG_SUCCESS forwards as INFO.
    SYSTEM_LEVELS = {
        LOG_DEBUG: logging.DEBUG,
        LOG_INFO: logging.INFO,
        LOG_SUCCESS: logging.INFO,
        LOG_WARNING: logging.WARNING,
        LOG_FAILURE: logging.ERROR,
    }
