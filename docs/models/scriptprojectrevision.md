# Script Project Revision

A Script Project Revision records an immutable snapshot of a Project's complete
source tree and the Script File configuration used when staging it. It
identifies the source and declarations used for validation and execution.

Its identity combines the Project, the source digest and the Script File
digest. Changing the enabled [Script Files](scriptfile.md) gives the same source
a different revision identity while reusing its stored content. Existing
validation verdicts still refer to their original snapshots.

Storage and validation services create revisions and manage their lifecycle.
Revisions are not edited directly.

## Fields

FK means foreign key. These fields describe stored model data, not client input.
All are managed by the plugin, including those not marked `system`.

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project. Cannot change after creation |
| `digest` | string | no | 64-character lowercase hexadecimal content address, assigned when the manifest is accepted. Required for stored source, absent for rejected content |
| `status` | choice | yes | `staging`, `materialized`, `storage_failed`, `validating`, `valid`, `invalid`, `active` or `retired` |
| `manifest` | JSON | no | Accepted source files, sorted by path. Each entry has `path`, `size` and `sha256` |
| `file_count` | integer | yes | Number of accepted source files |
| `total_size` | integer | yes | Combined size of accepted source files, in bytes |
| `validation_errors` | JSON | no | Records from the latest storage or validation step. An empty list is not proof of validity because an unvalidated revision can also have none |
| `discovered_scripts` | JSON | no | Classes recorded by successful validation, in publication order. Activation uses them to publish Script rows. May be empty |
| `last_validation_failure` | string | system | Why the last validation attempt reached no verdict, rather than errors in a completed verdict. Cleared when a verdict is reached |
| `script_file_snapshot` | JSON | yes | Enabled declarations frozen at staging, sorted by path. Each entry has a `script_file` primary key and canonical `source_path`. May be empty |
| `script_file_digest` | string | yes | 64-character lowercase hexadecimal address of the declaration snapshot. Part of revision identity |
| `validation_job` | FK | system | Job owning the current validation lease, retained as provenance when it records a verdict |
| `validation_started` | datetime | system | Time the owning validation claimed the revision |
| `activated` | datetime | no | Most recent time the revision became active |

Storage-time `validation_errors` entries contain `path`, a fixed `code` and a
human-readable `message`. The path is null for Project-wide limits such as
`too_many_files` or `project_too_large`.

Validation-time entries contain `source_path`, `code` and `message`, plus
`exception_type` and `traceback` when an exception was involved. Recorded errors
are sanitized to remove runtime namespaces, storage identities and cache paths.
Module references use Project-relative paths.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `ScriptProject` | yes | `on_delete=CASCADE`, reverse name `revisions` |

The Project points to its active revision through
`ScriptProject.active_revision`, with `on_delete=SET_NULL` and reverse name
`active_revision_for`. Removing that revision clears the pointer and leaves the
Project with no active source. `SET_NULL` also allows Project deletion to
cascade through its own revisions without a protected-reference cycle.

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/project-revisions/` |
| GraphQL | `netbox_script_project_revision` / `netbox_script_project_revision_list` |

REST and GraphQL are read-only. Use source and activation actions to change a
revision's lifecycle, not writes to its status field. Filter revisions by
`project_id`, `project` (the Project key), `status`, `digest` or
`script_file_digest`.

Neither interface exposes `manifest` or `script_file_snapshot`. These are full
stored documents, not lookup fields. The validation lease fields,
`validation_job` and `validation_started`, are also omitted. They record
internal ownership rather than editable configuration.

Revisions have a detail page, reached from the Project's **Revisions** tab, but
no standalone list, edit, delete or global-search view. The tab also offers:

- **Activate** for a `valid` or `retired` revision. It becomes active and
  publishes its [Scripts](netboxscript.md).
- **Deactivate** for the active revision. It retires the revision and its
  Scripts, leaving the Project with no active source.

Both actions require the owning Project's `activate` permission. Access through
the Revisions tab also requires revision view permission. Each action opens a
confirmation page, keeping its form separate from the table's bulk-action form.

Use **Deactivate** to stop serving a revision without deleting the Project.
Retired Script rows retain their Job history and administrator-owned `enabled`
settings, which are reused if the same classes are published again.

Under **Automatic if valid**, deactivation lasts only until the same source is
accepted again. The next reconciliation, Script File refresh or upload that
resolves to this revision activates it again, and so can a validation queued
before the deactivation. To keep it inactive, switch the Project to **Manual**
before deactivating.

Event serialization also uses the revision serializer when a request deletes
revisions, including a Project deletion that cascades to them.

The model supports change logging. Request-driven activation records changes.
Automatic activation in the validation Job does not use that change-logging
context. See [Event Rules](../event-rules.md#scripts-as-event-sources) for the
separate event-delivery rules.

## Recorded Scripts

`discovered_scripts` records what validation found when importing the source.
Each entry identifies the defining module and class, the Script File that
published it, and the class's display name, description and execution defaults.
Entries retain publication order.

The list is written in the same statement as the verdict, so a valid revision
has its discovery result recorded. An invalid verdict records an empty list.
[Activation](../runtime.md) builds [Script](netboxscript.md) rows from these
records rather than importing the source again.

A revision created before this field existed has no records to publish on
reactivation. Only validation writes the discovery list.

Unlike the manifest and declaration snapshot, this derived data has no digest.
Activation validates its shape before using it to rebuild Script rows. The
manifest and snapshot remain digest-bound inputs to execution. Validation is
the only supported writer of the discovery records.

## Script file snapshot

The snapshot freezes enabled Script File declarations when a revision is staged.
Later edits or disabling a declaration do not change that revision's verdict.

To use the current declarations with stored source, the storage service offers
a refresh operation. It creates or returns the revision for the same source
digest and the current Script File digest, without another upload.

A canonical builder creates the snapshot. Refresh, validation and activation
check it before use, including its shape, paths, ordering, uniqueness and digest.
Uniqueness checks include case collisions and paths that resolve to the same
module name. A failed check is revision corruption, not an invalid-content
verdict, and the operation is refused.

Explicit activation can still use a valid revision's frozen selection after
current declarations change, including when a declaration was disabled after
staging. Automatic activation additionally checks the Project's accepted source
and current selection. See [Script Project](scriptproject.md#limitations).
Script File rows are removed only by Project deletion, which also removes the
revisions.

## Status lifecycle

Storage, validation and activation are separate stages:

- **Storage** reaches `materialized` once the source is stored and verified
  against its manifest.
- **Validation** imports the snapshot's Script Files and runs discovery to
  reach `valid` or `invalid`. See [Runtime and Loading](../runtime.md) for the
  distinction between content errors and environment failures.
- **Activation** accepts only `valid` or `retired` revisions.

| From | To | Cause |
|---|---|---|
| (new) | `staging` | The manifest was accepted and storage writing begins |
| `staging` | `materialized` | Source was written and verified against its manifest |
| `staging` | `storage_failed` | A retryable storage write failed |
| `storage_failed` | `staging` | Re-staging identical content resumes the write |
| (new) | `invalid` | Content was rejected before anything was stored |
| `materialized` | `validating` | Validation claimed the revision |
| `validating` | `valid` or `invalid` | Validation reached a verdict |
| `validating` | `materialized` | An environment failure released the claim for retry |
| `validating` | `validating` | A newer validation reclaimed an expired lease |
| `valid` or `retired` | `active` | The revision was activated |
| `active` | `retired` | Another revision was activated, or this revision was deactivated |

`staging` covers storage writes and their retries. `validating` belongs to the
validation worker, so re-staging must not reset a revision that worker owns.
Stored content alone is not enough to activate a `materialized` revision.

Re-staging identical content can resume an interrupted or failed write. It does
not reopen a validation verdict. A revision marked `invalid` by validation
keeps that verdict and its errors. Changed source or a different Script File
selection gives it a different revision identity to validate.

A retired revision can be activated again after its stored source passes
verification.

### The validation lease

Validation claims a revision by setting `validating` and recording the owning
Job and claim time. An expired lease can be reclaimed by a newer validation Job,
regardless of the old Job's status. This handles workers that disappear without
updating their Job row. The configured Job timeout is shorter than the lease.

Lease expiry permits a new claim. It does not itself submit another validation
Job. Once the lease has expired, the next reconciliation, upload or script file
refresh of the project that resolves to the revision queues a new validation,
which claims it.

Transitions to `valid`, `invalid` or back to `materialized` check both
`validating` status and the owning Job. A worker whose lease was reclaimed
cannot write revision results or Script File discovery fields. A completed
verdict retains `validation_job` and `validation_started` as provenance. A
return to `materialized` clears them.

## Invariants

| Invariant | Enforcement |
|---|---|
| `project`, `digest`, `manifest`, `file_count`, `total_size`, `script_file_snapshot` and `script_file_digest` cannot change after creation | `save()` guard, no form or serializer permits writes to them |
| A Project has at most one revision for each source and Script File digest pair | Partial `unique_project_digest_script_files` database constraint, applied when a digest is set |
| Every revision except an invalid one carries a digest | `revision_requires_digest_unless_invalid` database check constraint |
| Rejected content has no digest and is not deduplicated | Staging stores a null digest, excluded from the partial uniqueness constraint |
| Only `valid` or `retired` revisions can be activated | The activation service raises `ActivationError` otherwise |
| A Project has at most one active revision | `unique_active_revision_per_project`, plus retirement of the previous revision in the locked activation transaction |
| Stored source matches its manifest before reuse or activation | `store.verify_revision_tree()` raises `RevisionCorruptError` on a mismatch |
| A stored declaration snapshot matches its digest before use | `validate_script_file_snapshot()` raises `RevisionCorruptError` on a mismatch |
| Recorded Scripts have a valid shape and execution defaults | `validate_discovered_scripts()` checks before the Project lock and again on the locked row, including a positive timeout in seconds and a valid Job notification choice |
| Revision inputs and discovery records did not change during storage verification | The locked row is compared with the verified digests, manifest, snapshot and recorded Scripts |
| Only the owning validation Job records a verdict | Final transitions filter on `validating` and the owning Job |

Lifecycle fields remain mutable: `status`, `validation_errors`,
`last_validation_failure`, `discovered_scripts`, `activated` and the lease
fields. Only the responsible services should update them.

Different rejected submissions can have the same accepted subset of files.
Leaving their digests null prevents those failed attempts from sharing a content
identity.

Code that bypasses validation, such as `QuerySet.update()` or raw SQL, must
preserve these invariants. Database constraints enforce digest uniqueness, not
immutability.

## Serialization

Staging, refreshing source under a different Script File selection, activation
and storage cleanup share a Project-scoped lock. It is keyed by the immutable
`storage_key` so changes to editable fields cannot change the lock identity.
These operations coordinate revision rows with stored content, which the
database cannot manage as part of its own transactions.

The lock is a session-level advisory lock. Storage I/O does not require a
long-running database transaction, and closing the owning database session
releases the lock. See
[Database connection pooling](../configuration.md#database-connection-pooling)
for deployment requirements.

**Deletion and cleanup use different boundaries.** Deleting a revision row does
not remove stored content synchronously. The cleanup Job takes the Project lock
and then checks whether any revision still references that content before
removing it. A reference check made only before acquiring the lock is not
sufficient.

REST Project deletion takes the Project lock before delegating to NetBox's
delete hook. Deleting a Project in the browser does not take it. In both cases,
each cascaded Script File deletion takes the Project write lock, which shares
the lock keyspace.

**Validation does not take the Project lock.** Claims and final transitions use
conditional updates tied to the owning Job. Imports run under the
[validation lease](#the-validation-lease) rather than holding the Project lock
while arbitrary source code executes.

If a concurrent deletion removes a revision during staging, staging reports the
vanished row instead of returning it. The deletion records cleanup intent for
the stored content. Cleanup failures still need attention as described below.

## Storage layout

Files use the configured storage backend, with one key per file beneath
`netbox-scripts/<storage_key>/revisions/<digest>/`. The Project owns
`storage_key`. No request, branch or schema changes the key, so every node
addresses the same content. See [Configuration](../configuration.md) for backend
setup and the storage trust boundary.

**Verification checks content, not just presence.** Stored source is checked
against its manifest after writing, before reuse and before activation.
Verification of a completed tree reports mismatches rather than repairing them.
Only manifest-listed keys are read, materialized or executed. Unlisted keys are
not imported or automatically reclaimed.

**Storage writes can be incomplete.** The plugin writes individual files, so a
revision prefix can exist before the whole tree is ready. Status records the
write's progress, and verification checks its result. When an interrupted write
is resumed, files matching their recorded size and checksum are kept. Missing
or mismatched files are written again.

**Different selections can share content.** Revisions with the same Project
and source digest but different Script File digests share one stored tree. The
deletion signal skips cleanup while another revision references that digest.
The cleanup Job repeats the reference check before removing content.

Deleting a revision captures the manifest's exact file keys before removing the
row. The cleanup Job and the deletion are recorded in the same database
transaction. Both commit or both roll back. Queue submission waits until commit,
and the deleting process performs no remote storage removal.

This transaction coupling uses the default database. Staging, activation and
deletion refuse other database aliases. Project deletion uses the same cleanup
path for its cascaded revisions. Bulk `QuerySet.delete()` also triggers it,
because registered deletion receivers prevent Django's signal-free fast-delete
path.

**Cleanup can be retried.** Missing keys count as already removed. A failed Job
logs the keys left behind and retains its full payload: storage key, digest and
file paths. Use that record to reconstruct the cleanup after resolving the
backend failure. The Job also rechecks branching routing. Unsafe routing fails
the Job without deleting content.

Content missed by cleanup remains in storage. On filesystem backends, empty
directories can remain after every file is removed because the Django storage
API has no directory-removal operation. Those empty directories contain no
source.

## Limitations

| Limitation | Impact |
|---|---|
| No standalone list page or global search | Use the Project's Revisions tab, or filter REST and GraphQL results by Project |
| Complete manifest and Script File snapshot documents are absent from REST and GraphQL | Reading those stored JSON documents requires database access. The Project's Revision Files tab displays the current revision's files |
| No user-facing revision-delete route | Revisions are removed when their Project is deleted |
| Only the active revision can be deactivated | `deactivate_revision()` checks the locked Project row and refuses other revisions |
