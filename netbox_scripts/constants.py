"""Module-level constants for the NetBox Scripts plugin."""

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
# No verdict has been reached yet, so a revision here may still become servable on its own.
PENDING_VERDICT_REVISION_STATUSES = ('staging', 'materialized', 'validating')
# A staging attempt may re-drive these. "validating" and "invalid" are deliberately absent,
# because both belong to project validation: storage must never re-drive a revision whose
# lifecycle it does not own, nor resurrect one that validation rejected.
RETRYABLE_REVISION_STATUSES = ('staging', 'storage_failed')
# A digest is recorded before the write completes, so these carry one while the store holds nothing.
UNSTORED_REVISION_STATUSES = ('staging', 'storage_failed')

# A revision records why a validation could not reach a verdict, and that text reaches an operator
# through a refusal rendered on a page, so it is bounded rather than allowed to hold a traceback.
MAX_VALIDATION_FAILURE_LENGTH = 500

# Persisted bounds on a published Script. The model fields the discovery snapshot
# feeds declare these lengths, and validation enforces them while it still owns a verdict,
# so an over-long name is a content failure rather than a database error at activation.
# Every bounded column the snapshot feeds belongs here, not only the identity, because
# activation writes all of them in one statement.
MAX_SCRIPT_CLASS_NAME_LENGTH = 79
MAX_SCRIPT_DISPLAY_NAME_LENGTH = 255
MAX_SCRIPT_MODULE_PATH_LENGTH = 1000

# Validation lease bounds, in seconds. The job timeout caps one validation run inside the
# worker, and the lease is deliberately longer, because reclaim is purely time based: a
# killed worker leaves its Job row running forever, so only an expired lease can hand the
# claim on. The margin absorbs clock skew and the tail of a run that outlived its own
# timeout signal.
VALIDATION_JOB_TIMEOUT = 10 * 60
VALIDATION_LEASE_SECONDS = 30 * 60

# The grace covers an ordinary queue backlog and a run still in progress, so the sweep names
# only a handoff that was genuinely lost.
STALLED_CLEANUP_GRACE_SECONDS = 60 * 60
