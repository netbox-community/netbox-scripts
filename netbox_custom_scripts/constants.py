"""Module-level constants for the Custom Scripts plugin."""

# Default storage limits applied to a project's source tree when a revision is staged.
# A deployment can override any of these through the plugin's PLUGINS_CONFIG settings.
DEFAULT_MAX_FILE_SIZE = 10 * 1024 * 1024
DEFAULT_MAX_PROJECT_SIZE = 100 * 1024 * 1024
DEFAULT_MAX_FILE_COUNT = 1000

# Revision lifecycle groupings that the storage and activation services branch on. The
# values mirror RevisionStatusChoices and are spelled out here rather than imported, so this
# module stays free of imports and the plugin package can load before Django app setup. A
# test asserts every member below is a real choice value, so the two cannot drift apart.
#
# Only a revision that passed project validation may be served.
ACTIVATABLE_REVISION_STATUSES = ('valid', 'retired')
# The source tree of a revision in one of these states is in the store.
STORED_REVISION_STATUSES = ('materialized', 'valid', 'active', 'retired')
# A staging attempt may re-drive these. "validating" and "invalid" are deliberately absent,
# because both belong to project validation: storage must never re-drive a revision whose
# lifecycle it does not own, nor resurrect one that validation rejected.
RETRYABLE_REVISION_STATUSES = ('staging', 'storage_failed')
