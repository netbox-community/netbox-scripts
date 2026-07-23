"""Module-level constants for the Custom Scripts plugin."""

# Default storage limits applied to a project's source tree when a revision is staged.
# A deployment can override any of these through the plugin's PLUGINS_CONFIG settings.
DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_MAX_PROJECT_SIZE = 100 * 1024 * 1024
DEFAULT_MAX_FILE_COUNT = 1000
