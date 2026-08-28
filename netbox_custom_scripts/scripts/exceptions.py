__all__ = ('AbortScript',)


class AbortScript(Exception):
    """
    Raised inside run() to end a Custom Script early.

    The exception carries a message that explains the stop.
    """
