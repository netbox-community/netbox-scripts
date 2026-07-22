"""
Authoring API for Custom Scripts.

Everything a script author needs is exported here: the ``Script`` base classes, the
variable classes, logging vocabulary, and exceptions.
"""

from .exceptions import AbortScript
from .logging import LogLevelChoices

__all__ = (
    'AbortScript',
    'LogLevelChoices',
)
