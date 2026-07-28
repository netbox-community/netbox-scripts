# Custom Script Project Revision

A Custom Script Project Revision is one immutable snapshot of a Custom Script
Project's complete source tree. A revision records what was staged, not how it
is served, so a job can be replayed against exactly the tree it ran on.

Revisions are created and moved through their lifecycle by the plugin's storage
service. They are not edited directly.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Custom Script Project, frozen after creation |
| `digest` | string | no | 64-character lowercase hexadecimal content address, set as soon as the manifest is accepted. Required once the tree is stored, absent when content was rejected |
| `status` | choice | yes | `staging`, `materialized`, `storage_failed`, `validating`, `valid`, `invalid`, `active`, or `retired` |
| `manifest` | JSON | no | Sorted list of accepted source files, each with its `path`, `size`, and `sha256` |
| `file_count` | integer | yes | Number of accepted source files |
| `total_size` | integer | yes | Combined size in bytes of every accepted source file |
| `validation_errors` | JSON | no | Records from the most recent storage or validation step. An empty list does not by itself mean the revision is valid, because a revision that has not been validated yet also has none |
| `activated` | datetime | no | When the revision last became the project's active revision |

Each `validation_errors` record carries a `path`, a fixed `code`, and a
human-readable `message`. The `path` is null for a project-wide limit such as
`too_many_files` or `project_too_large`.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `CustomScriptProject` | yes | `on_delete=CASCADE`, reverse name `revisions` |

The owning project also points back at one of its revisions through
`CustomScriptProject.active_revision`, an `on_delete=PROTECT` reference with the
reverse name `active_revision_for`. An active revision therefore cannot be
deleted while it is being served.

## API

| Surface | Endpoint or field |
|---|---|
| REST | none in this release |
| GraphQL | none in this release |

Revisions carry no UI, REST, GraphQL, filterset, or global-search surface yet.

The model is change-logged, so entries will appear in NetBox's change log once a
request-bound code path stages or activates a revision. Nothing in this release
provides one, so no entries are recorded yet.

## Status lifecycle

Storing a source tree and judging it fit to execute are separate steps, owned by
separate services.

- **The storage service** takes a revision as far as `materialized`, meaning the
  tree is stored and matches its manifest.
- **Project validation** promotes a materialized revision to `valid` or
  `invalid`, based on Python imports, declared entrypoints, dependency checks,
  and Script discovery. That service arrives with the project package loader, so
  in this release nothing advances past `materialized`.
- **The activation service** accepts only `valid` or `retired` revisions.

| From | To | Cause |
|---|---|---|
| (new) | `staging` | The manifest was accepted and the write begins |
| `staging` | `materialized` | The source tree was written and verified against its manifest |
| `staging` | `storage_failed` | The storage write failed, which is retryable |
| `storage_failed` | `staging` | Re-staging identical content resumes the write |
| (new) | `invalid` | The submitted content was rejected, so nothing was written |
| `materialized` | `validating` | Project validation started |
| `validating` | `valid` or `invalid` | Project validation reached a verdict |
| `valid` or `retired` | `active` | The revision was activated |
| `active` | `retired` | Another revision of the same project was activated |

`staging` covers the whole storage write, including a retry, and `validating` belongs to
project validation. Keeping them apart is what stops a concurrent re-stage from rewriting a
revision the validator is holding and pushing it back to `materialized`.

`materialized` is deliberately not activatable: stored bytes are not evidence
that a project imports, so a tree that was merely written can never be served.

Re-staging identical content resumes an interrupted or failed write, but never
reopens a verdict. A revision that project validation marked `invalid` keeps its
errors and stays `invalid`, because only the validator may move it.

A retired revision can be activated again, since its source tree is still in
the store and is verified before it is served.

## Invariants

| Invariant | Enforcement |
|---|---|
| `project`, `digest`, `manifest`, `file_count`, and `total_size` cannot change after creation | `save()` guard, no form or serializer exposes them |
| A project cannot hold two revisions with the same digest | Partial `unique_project_digest` database constraint, applied only when a digest is set |
| A revision whose tree is stored must have a digest | `stored_revision_requires_digest` database check constraint |
| An invalid revision carries no digest and is never content-deduplicated | The staging service stores a null digest, which the partial constraint ignores |
| Only `valid` or `retired` revisions may be activated | The activation service raises `ActivationError` otherwise |
| A project has at most one active revision | `unique_active_revision_per_project` database constraint, plus the activation service retiring the previous one inside a locked transaction |
| A stored tree still matches its manifest before it is reused or activated | `store.verify_revision_tree()`, which raises `RevisionCorruptError` |

`status`, `validation_errors`, and `activated` stay mutable, because they are the
lifecycle fields the storage service moves.

Two different rejected trees can share the same accepted subset of files. Storing
them with a null digest is what keeps them from colliding on one content address.

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must uphold
these invariants themselves. The database constraint enforces digest uniqueness
but not immutability.

## Storage layout

A revision's files live in the storage backend the plugin is configured to use,
one key per file, under
`netbox-custom-scripts/<storage_key>/revisions/<digest>/`, where `storage_key`
belongs to the owning project. The key is a pure function of those values, with
no request, branch, or schema context, so a stored file resolves identically on
every node and in every pod. See [Configuration](../configuration.md) for the
backend and the trust boundary that applies to it.

Nothing about a key existing is taken as proof that its content is intact. A
stored tree is verified against its manifest as soon as it is written, before a
staging call reuses it, and before a revision is activated, and a mismatch is
reported rather than repaired, so tampering cannot pass silently. The manifest
is the whole boundary: only the keys it names are ever read, materialized, or
executed, so a key it does not describe is inert rather than importable.
Reclaiming such strays belongs to the housekeeping reconciler planned for a
later release.

No backend can make a whole tree appear at once, so a revision being written is
visible under its prefix while it is still incomplete. The status is what says
whether the content is finished, and verification is what confirms it. That also
makes a write safe to repeat: a key already holding the recorded size and
checksum is left alone, and one holding anything else is replaced, so an
interrupted write is completed by the next attempt rather than blocking it.

Deleting a revision reclaims its stored content through a background cleanup
job. The exact keys to remove are captured from the revision's manifest while
its row still exists, and the cleanup job carrying that payload is written in
the same database transaction that deletes the row, so deletion and cleanup
intent commit or roll back together and a rolled-back delete leaves the content
in place with no orphaned job behind. Only the handoff to the queue waits for
the commit, which keeps remote storage I/O out of the deleting process. That
coupling holds on the default database, which is where these models live.
Staging, activation, and deletion refuse any other database alias, so no
revision can exist whose deletion could not record its cleanup. Deleting a
project works the same way, because the cascade deletes each of its revisions
and every one records its own cleanup. Bulk `QuerySet.delete()` reclaims
storage too, because registering the cleanup receivers rules out Django's
signal-free fast-delete path.

Cleanup is idempotent and observable. A key that is already gone counts as
removed, so a cleanup that failed partway can be run again and finishes the
remainder. A failure surfaces as a failed background job whose log names the
keys it left behind, and the job keeps its full cleanup payload, the storage
key, digest, and file paths, so the work can be reconstructed and run again
once the backend is reachable. The job also repeats the branching routing
check before it removes anything, and a run that finds routing unsafe fails
while leaving the content in place. Reclaiming
anything the jobs miss is left to a future housekeeping reconciler. On a
backend that keeps real directories, such as a local filesystem, removing every
key can leave the empty directories behind, since the Django storage API has no
way to remove one. They hold no content and the reconciler is the right owner
for them.

## Limitations

| Limitation | Impact |
|---|---|
| No UI, REST, or GraphQL surface | Revisions can only be created by the storage service, which no user-facing view calls yet |
| Nothing advances past `materialized` | Project validation arrives with the package loader, so no revision can be activated in this release |
| An active revision cannot be deleted | Its project protects it. Activate another revision first, or delete the project |
| Staging is not serialized against itself or against deletion | Concurrent staging of one digest, or a project deleted mid-write, can leave the database and the store briefly disagreeing. The same boundary owns the queued-cleanup race: content re-staged while a deleted twin's cleanup Job is still pending can be removed by that Job once it runs. No caller in this release runs concurrently, and one shared locking model arrives with the first ones |
