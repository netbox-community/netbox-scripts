# Script Project Revision

A Script Project Revision is one immutable snapshot of a Script
Project's complete source tree together with the entrypoint configuration it
was staged under. A revision records what was staged, not how it is served, so
a job can be replayed against exactly the tree it ran on.

Revision identity is the project, the source digest, and the entrypoint
digest. The same source tree staged under a changed set of enabled
[Script Files](scriptfile.md) is a new, separately validatable
revision that reuses the stored content, which is what keeps a validation
verdict meaningful: fixing a module declaration produces a fresh revision to
validate instead of silently changing what an existing verdict was about.

Revisions are created and moved through their lifecycle by the plugin's storage
and validation services. They are not edited directly.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project, frozen after creation |
| `digest` | string | no | 64-character lowercase hexadecimal content address, set as soon as the manifest is accepted. Required once the tree is stored, absent when content was rejected |
| `status` | choice | yes | `staging`, `materialized`, `storage_failed`, `validating`, `valid`, `invalid`, `active`, or `retired` |
| `manifest` | JSON | no | Sorted list of accepted source files, each with its `path`, `size`, and `sha256` |
| `file_count` | integer | yes | Number of accepted source files |
| `total_size` | integer | yes | Combined size in bytes of every accepted source file |
| `validation_errors` | JSON | no | Records from the most recent storage or validation step. An empty list does not by itself mean the revision is valid, because a revision that has not been validated yet also has none |
| `discovered_scripts` | JSON | no | Custom Scripts the most recent successful validation published, in publication order. May be empty |
| `script_file_snapshot` | JSON | yes | Enabled module declarations frozen at staging time, each with its `script_file` primary key and canonical `source_path`, sorted by path. May be empty |
| `script_file_digest` | string | yes | 64-character lowercase hexadecimal address of the snapshot, part of the revision identity |
| `validation_job` | FK | system | Owner of the current validation lease, kept on the verdict as its provenance |
| `validation_started` | datetime | system | When the owning validation claimed the revision |
| `activated` | datetime | no | When the revision last became the project's active revision |

Each storage-time `validation_errors` record carries a `path`, a fixed `code`,
and a human-readable `message`. The `path` is null for a project-wide limit
such as `too_many_files` or `project_too_large`. Validation-time records
carry a `source_path`, a fixed `code`, a `message`, and where an exception was
involved its `exception_type` and a `traceback`. All of them are sanitized:
runtime namespaces, storage identities, and cache paths never appear, module
references read project-relative.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `ScriptProject` | yes | `on_delete=CASCADE`, reverse name `revisions` |

The owning project also points back at one of its revisions through
`ScriptProject.active_revision`, an `on_delete=SET_NULL` reference with the
reverse name `active_revision_for`. Deleting the revision a project is serving
clears the pointer and leaves the project serving nothing, which is the state it
starts life in. The pointer cannot be `PROTECT`: a project's revisions cascade
when it is deleted, so protecting one of them would have the project protect
itself, and no project with an active revision could ever be deleted.

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/project-revisions/` |
| GraphQL | `netbox_script_project_revision` / `netbox_script_project_revision_list` |

Both surfaces are read-only. A revision is produced by ingestion and moved
through its lifecycle by the storage and validation services, so every write
method is refused at the router and activation stays an action rather than a
writable status field. Filter the list by `project_id`, `project` (the project
key), `status`, `digest`, or `script_file_digest`.

Two fields are deliberately absent from both surfaces. The `manifest` and the
`script_file_snapshot` are stored documents rather than lookup keys, large enough
to dominate a list response and internal to how content is addressed. The
validation lease fields are absent for the same reason: they are a fencing
mechanism, not user-facing state. An advanced diagnostic surface is the right
owner for all four.

Revisions carry no list page, edit route, delete route, or global-search
surface. A revision has a detail page, reached from the project's **Revisions**
tab, which also renders two actions per row:

- **Activate** on any revision whose status is `valid` or `retired`, which puts
  it into service and publishes its [Custom Scripts](netboxscript.md).
- **Deactivate** on the revision in force, which retires it, leaves the project
  serving nothing, and retires its Custom Scripts.

Both need the owning project's change permission, because what they change is
what the project serves. Each opens a confirmation page that posts back, rather
than acting straight from the table, because the table sits inside the bulk-action
form and a nested form would not survive the browser. Deactivation is the only way to
stand a project down to serving nothing once it has served something. Retiring
the scripts rather than deleting them is what lets a later activation return the
same rows, with their Job history and with whatever `enabled` an administrator
left them at.

A serializer exists all the same, without a route to serve. Event serialization
resolves a serializer by model name, and it runs on any request that deletes a
revision, which a project delete does by cascade. It omits the `url` and
`display_url` fields the other models expose, because both reverse a detail route
and a revision has none.

The model is change-logged. Activating through the Project's **Activate** button
is a request-bound path, so it records entries. Automatic activation happens
inside a job, where NetBox records no change-log entries at all, so a project
whose policy activates automatically leaves none.

## Recorded Custom Scripts

`discovered_scripts` is what project validation learned by importing the tree:
one record per published class, in publication order, each carrying the defining
module path and class name, the entrypoint that published it, and the display
name, description, and execution defaults read from the class. It is written once,
in the same statement as the verdict, so no reader ever sees a valid revision
without it. An invalid verdict records an empty list.

[Activation](../runtime.md) derives [Custom Script](netboxscript.md) rows from
it, which is why a revision from before this field existed publishes nothing when
re-activated: it has no record to derive from, and only re-validation writes one.

Unlike the manifest and the entrypoint snapshot, it carries no digest. Those two
are inputs execution trusts, so they are bound to an address that proves they are
unchanged. This one is derived data that activation rebuilds rows from rather than
content it executes, so a shape validator on the return trip is the whole
requirement. Validation is its only writer, so a value that fails that check
means the row was changed outside that path.

## Entrypoint snapshot

The snapshot freezes the project's enabled module declarations at staging time,
so a verdict is always about a fixed set of entrypoints. Editing, disabling, or
deleting a Script File never changes an existing revision. To validate
stored content under the declarations as they are now, the storage service
offers a refresh operation that creates or returns the revision row for the
same source digest and the current entrypoint digest, without the content being
uploaded again.

Like the manifest, the snapshot is persisted data that later becomes
authoritative input, so it is never trusted on the return trip. A canonical
builder writes it and a validator checks its shape, paths, ordering, uniqueness
including letter-case collisions and paths that would import under one module
name, and digest binding everywhere it becomes
authoritative: the refresh operation, project validation, and activation. A
snapshot that fails is revision corruption and never a content verdict, so
tampering fails closed.

A valid revision whose snapshot references a since-deleted module row remains
activatable. The snapshot is the immutable contract, module deletion is not
restricted by it.

## Status lifecycle

Storing a source tree and judging it fit to execute are separate steps, owned by
separate services.

- **The storage service** takes a revision as far as `materialized`, meaning the
  tree is stored and matches its manifest.
- **Project validation** promotes a materialized revision to `valid` or
  `invalid` by importing every entrypoint in the snapshot and running Custom
  Script discovery on it. See [Runtime and Loading](../runtime.md) for what
  makes a revision invalid and what counts as environment trouble instead.
- **The activation service** accepts only `valid` or `retired` revisions.

| From | To | Cause |
|---|---|---|
| (new) | `staging` | The manifest was accepted and the write begins |
| `staging` | `materialized` | The source tree was written and verified against its manifest |
| `staging` | `storage_failed` | The storage write failed, which is retryable |
| `storage_failed` | `staging` | Re-staging identical content resumes the write |
| (new) | `invalid` | The submitted content was rejected, so nothing was written |
| `materialized` | `validating` | Project validation claimed the revision |
| `validating` | `valid` or `invalid` | Project validation reached a verdict |
| `validating` | `materialized` | Environment trouble rolled the claim back, retryable |
| `validating` | `validating` | An expired lease was reclaimed by a newer validation run |
| `valid` or `retired` | `active` | The revision was activated |
| `active` | `retired` | Another revision of the same project was activated, or this one was deactivated |

`staging` covers the whole storage write, including a retry, and `validating` belongs to
project validation. Keeping them apart is what stops a concurrent re-stage from rewriting a
revision the validator is holding and pushing it back to `materialized`.

`materialized` is deliberately not activatable: stored bytes are not evidence
that a project imports, so a tree that was merely written can never be served.

Re-staging identical content resumes an interrupted or failed write, but never
reopens a verdict. A revision that project validation marked `invalid` keeps its
errors and stays `invalid`, because only the validator may move it. Fixing the
content or the declarations produces a new revision identity to validate
instead.

A retired revision can be activated again, since its source tree is still in
the store and is verified before it is served.

### The validation lease

A validation claims its revision by moving it to `validating` while recording
the owning background job and the claim time. The claim is reclaimable purely
by age: a worker killed without warning leaves its job row running forever, so
after the lease expires a newer run may take the claim over regardless of what
the old job row says. The job timeout is deliberately shorter than the lease,
so a run is stopped before its claim can be handed on.

Every final transition, to `valid`, to `invalid`, and the roll-back to
`materialized`, is fenced on the owning job: a stale worker resuming after its
lease was reclaimed matches nothing and commits nothing, neither revision
fields nor module discovery results. The verdict keeps `validation_job` and
`validation_started` as its provenance, only the roll-back clears them.

## Invariants

| Invariant | Enforcement |
|---|---|
| `project`, `digest`, `manifest`, `file_count`, `total_size`, `script_file_snapshot`, and `script_file_digest` cannot change after creation | `save()` guard, no form or serializer exposes them |
| A project cannot hold two revisions with the same digest and entrypoint digest | Partial `unique_project_digest` database constraint, applied only when a digest is set |
| A revision whose tree is stored must have a digest | `stored_revision_requires_digest` database check constraint |
| An invalid revision from rejected content carries no digest and is never content-deduplicated | The staging service stores a null digest, which the partial constraint ignores |
| Only `valid` or `retired` revisions may be activated | The activation service raises `ActivationError` otherwise |
| A project has at most one active revision | `unique_active_revision_per_project` database constraint, plus the activation service retiring the previous one inside a locked transaction |
| A stored tree still matches its manifest before it is reused or activated | `store.verify_revision_tree()`, which raises `RevisionCorruptError` |
| A persisted snapshot is still the one its digest addresses before it becomes authoritative | `validate_script_file_snapshot()`, which raises `RevisionCorruptError` |
| A persisted list of published Custom Scripts still has a shape a build could produce | `validate_discovered_scripts()`, checked before the project lock and again on the locked row |
| Neither the tree nor what it publishes changed while the tree was being verified | The locked row is compared against the verified one, digests, manifest, snapshot, and published scripts alike |
| Only the owning validation run may record a verdict | Every final transition filters on `validating` and the owning job |

`status`, `validation_errors`, `discovered_scripts`, `activated`, and the lease
fields stay mutable, because they are the lifecycle fields the storage and
validation services move.

Two different rejected trees can share the same accepted subset of files. Storing
them with a null digest is what keeps them from colliding on one content address.

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must uphold
these invariants themselves. The database constraint enforces digest uniqueness
but not immutability.

## Serialization

Revision rows and stored content are two systems, and the database cannot see the second one.
Every operation that touches a project's stored content therefore holds a lock scoped to that
project while it does: staging, restaging under a new entrypoint configuration, activation, and
the cleanup that reclaims content. The lock is keyed on the project's immutable storage key, so
it names the content itself and cannot be moved by anything an author edits.

It is a PostgreSQL session-level advisory lock rather than a row lock, because these operations
hold conversations with the storage backend and a row lock would keep a database transaction
open for their duration. A session lock is released when the connection drops, so a worker that
dies without warning frees its own claim and no reclaim timer is needed.

Two operations deliberately stay outside it:

- **Deleting a revision or a project takes no lock.** The cleanup job is what decides whether
  stored content is still claimed, and it rechecks for a referencing row *under* the lock
  before removing anything. A bare check before the lock would lose to a revision staged
  between the check and the delete.
- **Validation holds it only for its row transitions.** Importing a revision's entrypoints runs
  arbitrary project code and can take minutes, and the [validation lease](#the-validation-lease)
  plus job fencing already own that span. Holding the project lock across a whole run would
  queue every upload to that project behind it.

One consequence a caller has to handle: a project deleted while a staging call is inside its
write window cascades that revision away, and staging reports the vanished row rather than
returning one that no longer exists. Its content is reclaimed by the cleanup the delete
recorded, so nothing leaks.

## Storage layout

A revision's files live in the storage backend the plugin is configured to use,
one key per file, under
`netbox-scripts/<storage_key>/revisions/<digest>/`, where `storage_key`
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

Two revisions that share one source digest under different entrypoint digests
share one stored content tree. Deletion accounts for that: the deletion signal
skips enqueueing cleanup while another revision of the project still references
the digest, and the cleanup job repeats that check when it runs, leaving shared
content in place.

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
| No list page and no global search | A revision is reached through its project, on the Revisions tab or by filtering the REST and GraphQL surfaces by project |
| The manifest and the entrypoint snapshot are absent from both API surfaces | Reading a revision's file list or its frozen declarations needs the database until a diagnostic surface exists. The project's Files tab lists the current revision's files in the UI |
| Validation is not enqueued automatically | Staging leaves a revision `materialized`. Code has to enqueue the validation job, no production trigger wires it up yet |
| A revision cannot be deleted through any user-facing surface | It has no delete route of its own. Revisions go away when their project does |
| Only the revision a project is serving can be deactivated | `deactivate_revision()` compares against the locked project row and refuses otherwise |
| Staging is not serialized against itself or against deletion | Concurrent staging of one digest, or a project deleted mid-write, can leave the database and the store briefly disagreeing. The same boundary owns the queued-cleanup race: content re-staged while a deleted twin's cleanup Job is still pending can be removed by that Job once it runs. No caller in this release runs concurrently, and one shared locking model arrives with the first ones |
