"""The legacy report surface, which exists only to be recognized and refused."""

from ..scripts.base import Script

__all__ = ('Report',)


class Report(Script):
    """Legacy report base class. Discovery refuses every subclass of it."""

    _netbox_script_report = True
