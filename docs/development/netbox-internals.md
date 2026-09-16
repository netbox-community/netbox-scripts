# NetBox internals this plugin depends on

NetBox states its plugin contract plainly: anything not covered by the plugin
development documentation is not part of the supported plugins API and may change
without warning. This page lists every place this plugin reaches past that line,
so an upgrade that breaks one is a single grep rather than a hunt.

Everything else the plugin uses is either documented plugin API or Django, which
the plugin contract allows explicitly.

## The list

| Symbol | Where we use it | What it gives us |
|---|---|---|
| `netbox.registry.registry['request_processors']` | `execution.py` | The registered per-request context managers, applied around a script run so its changes behave as they do in a request |
| `netbox.context_managers.event_tracking` | `execution.py` | Change attribution and event queuing. Skipped deliberately for a dry run |
| `netbox.context.current_request` | `execution.py` | Read before a run and restored afterwards, so a failed or nested run leaves no stale request behind |
| `core.signals.clear_events` | `execution.py` | Discards queued events when a run is abandoned |
| `django.db.router.db_for_write` on a change-logged core model | `execution.py` | Which database change-logged writes go to, which is a branch schema while a branch is active. The model is asked of NetBox Branching first, so one that stopped being branch-aware is named rather than read as the default alias |
| `utilities.exceptions.AbortScript` | `execution.py` | The abort raised by a script carried over unchanged from the built-in feature, caught beside this plugin's own so either one ends a run cleanly. NetBox documents it for script authors rather than for plugins, and it is expected to go with the built-in feature at v5.0 |
| `utilities.request.copy_safe_request` | `views/scripts.py`, `api/views.py` | A picklable, sensitive-header-stripped copy of the request, so it can travel to a worker |
| `netbox.api.viewsets.mixins.discard_events_on_rollback` | `api/views.py` | Clears request events when an entire declaration-writing API action rolls back. Never used for a disposable-write probe |
| `utilities.exceptions.PermissionsViolation` | `permissions.py`, `api/views.py` | Rejects child writes outside the actor's object-permission scope, using core's enclosing rollback path |
| `netbox.views.generic.BulkEditView.pre_save_operations` | `views/projects.py` | The per-object hook core calls inside its bulk edit transaction before each save, where the source-field gate reads the undocumented `_nullify` request list that core itself consumes for a Set null tick |
| `utilities.rqworker.get_queue_for_model` | `api/views.py`, `jobs.py` | Resolves the same configured queue for worker checks and enqueue operations |
| `rq.utils.parse_timeout` | `runtime/introspection.py` | Reads an author's `job_timeout` with the grammar RQ itself applies, so `"30m"` means the same to the plugin as to the worker that enforces it |
| `rq.exceptions.TimeoutFormatError` | `runtime/introspection.py` | The refusal RQ raises for a timeout string it cannot parse, which becomes a script metadata error rather than an unhandled failure |
| `rq.timeouts.JobTimeoutException` | `jobs.py`, `validation.py`, `runtime/introspection.py` | The worker's own deadline. It is propagated rather than classified, so a run that ran out of time releases its lease instead of recording a permanent verdict |
| `utilities.rqworker.any_workers_for_queue` | `api/views.py` | Whether a worker is live for the queue, so a REST run that nothing could pick up is refused rather than queued |
| `utilities.exceptions.RQWorkerNotRunningException` | `api/views.py` | The 503 that refusal answers with, which is what NetBox's own run endpoint returns |
| `netbox.api.authentication.TokenPermissions` | `api/views.py` | The permission class the REST run action subclasses, so a POST resolves to the run permission rather than to add |
| `extras.models.ScriptModule` and its manager | `migration/source.py` | Every built-in script module. A proxy on `ManagedFile` whose manager admits both the `scripts` and `reports` roots, so legacy Reports are already rows |
| `extras.models.Script` through `module.scripts` and its manager | `migration/source.py` | Which script classes each built-in module publishes today, and which of them a report owns, queried by module file root |
| `core.models.ManagedFile` fields `file_root`, `file_path`, `data_path`, `data_source` | `migration/source.py` | Where a module's bytes are and which repository path it came from. NetBox marks this model `_netbox_private` |
| `core.choices.ManagedFileRootPathChoices.SCRIPTS` and `.REPORTS` | `migration/source.py` | The two roots the proxy manager admits. Content reads filter to the first, the reference readers subtract what the second owns, and it is counted so that Reports are visibly outside this migration |
| `extras.models.mixins.PythonModuleMixin.python_name` | `migration/source.py` | The bare stem of a module's file name, or its parent directory for an `__init__.py`. Not a dotted path, so it is a label rather than something importable |
| `storages['scripts']` | `migration/source.py` | The stored bytes of one built-in module, opened by `file_path` because that is what the built-in loader opens |
| `extras.models.EventRule.action_object_type` | `migration/source.py` | How many Event Rules a migration would have to repoint |
| `users.models.ObjectPermission.object_types` | `migration/source.py` | How many permissions name the built-in models |
| `core.models.Job.object_type` | `migration/source.py` | How much Job history exists, including what is scheduled or recurring |
| `extras.models.ScriptModule.jobs` and `.event_rules` | `migration/source.py` | Whether deleting one module would take Job history or an Event Rule with it. Both are `GenericRelation`s, so Django's collector deletes those rows rather than orphaning them |
| `users.models.ObjectPermission.enabled` | `migration/cutover.py` | Withdrawing every grant on the built-in feature, which is what the shipped cutover fences with |
| `extras.models.EventRule.enabled` | `migration/cutover.py`, `migration/references.py` | Taking a rule out of service for the handover, and putting it back once its action and sources name plugin rows |
| `core.models.Job.terminate` | `migration/cutover.py` | Failing a queued run closed. Used rather than an `update()` so the owner is notified, and there is no cancelled status to set |
| `django_rq.get_queue` and `rq.job.Job.fetch` / `.delete` | `migration/cutover.py` | A queued run's input, which lives only on the RQ task because `Job.enqueue()` keeps it off the row, and then dropping that task so nothing can execute it |
| `rq.exceptions.NoSuchJobError` | `migration/cutover.py` | The miss RQ raises when a queued run's task is already gone. The capture records that run's input as unrecoverable, and the closure treats the task as already dropped |
| `core.models.AutoSyncRecord` | `migration/cutover.py` | Deregistering the built-in source, so no later synchronization rewrites it. Filtered on the **concrete** `ManagedFile` type, the inverse of the proxy rule below |
| `extras.models.EventRule.action_type`, `.action_object_type`, `.action_object_id`, `.object_types` | `migration/references.py` | Repointing a rule onto the Script that replaced its built-in one. `full_clean()` first, because a rule can be invalid for reasons that predate the migration |
| `users.models.ObjectPermission` creation, `.actions`, `.object_types`, `.users`, `.groups` | `migration/references.py` | Moving a grant onto the plugin's models, and splitting one that also named something else |
| `users.models.Group` | `migration/references.py` | Resolving a captured grant's groups to live rows before the sibling permission takes them, so a group deleted since the cutover is reported rather than written as a raw key |
| `core.models.Job.object_type` / `.object_id` update | `migration/references.py` | Repointing run history, batched, and done before anything is deleted because a Script's jobs go with it |
| `extras.models.ScriptModule.delete` | `migration/cleanup.py` | Retiring a module and its stored source. Called per instance, because `QuerySet.delete()` does not call the model's `delete()`, which is what removes the file |

These dependencies are grouped by responsibility. If NetBox adds a documented execution context, replacing
the execution rows is a change to `execution.py` alone. The API rows cover run dispatch and transactional declaration authorization.
The permission helper uses the same refusal exception as core's object-edit views.

Every row is also a probe in `scripts/check_netbox_internals.py`, which resolves each
symbol after `django.setup()` with no database and reports a failure under the row it
belongs to. CI runs it against NetBox's `feature` branch as a blocking job while the test
leg on that branch stays advisory, so a crossing that disappears upstream fails a pull
request here before any release this plugin supports carries the change. A probe checks
that a symbol is present, not its signature or its relation kind, which the full suite on
the pinned ref covers. The script also fails when NetBox skipped the plugin at settings load
for being outside its version range, because a green run over a plugin that cannot load
there would say nothing. Adding a row to this table means adding a probe there, in the same
order, and a test holds the two in step. Locally:

```bash
PYTHONPATH=$PWD/testing NETBOX_CONFIGURATION=configuration \
  python scripts/check_netbox_internals.py --netbox /path/to/netbox/netbox
```

Every migration row that **reads** the built-in feature is confined to
`migration/source.py`, which is the only module in the plugin that finds those rows
at all. The three modules below it **write** to rows that `source.py` handed them,
which is the split the tier is built on: finding a row needs to know how the
built-in feature is shaped, and deciding what to do with it does not.

Two of the migration rows are easy to get subtly wrong, so they are worth stating.
The stored bytes are opened by `file_path` and never by `full_path`: `full_path`
prefixes a root that only a filesystem backend has, and the built-in loader itself
opens the relative path. And both content types have to be resolved with
`for_concrete_model=False`, because a Job and an Event Rule record the **proxy**,
so a sweep that resolves the concrete model reports zero on an installation full of
references.

The bulk import gate in `views/projects.py` reads the instance core is about to save
rather than the form's `cleaned_data`, because core reports a blank CSV cell as omitted
and keeps the stored value. That reading holds only while core deletes the form fields
a record does not name, which is what stops an absent column from constructing
`data_source` or `data_path` as empty. Core documents neither behaviour. A change to
either would refuse every update record from a user without `activate`, a closed
failure rather than a bypass.

## Why they are not avoidable

The behaviour they provide is not optional. Without the request processors a
script's database changes get no user attribution and its events never reach the
event pipeline, which silently breaks change logging, webhooks and Event Rules for
everything the script touches. Without the router probe a script running inside a
branch writes through a connection no transaction of ours has opened, so a dry run
may not revert and a failure may not roll back. Without a safe request copy the run
cannot reach a worker at all.

The `AbortTransaction` NetBox uses for dry-run rollback is the one internal we
declined to depend on. It is a bare exception used purely as a rollback trigger,
so the plugin defines its own equivalent and behaves identically.

## The advisory-lock namespaces

Two things the plugin holds are not symbols and so are not in the table above.
It serializes on two-integer PostgreSQL advisory locks in **two** namespaces:
`(770100, .)` per project, keyed by `storage_key` in `storage/locks.py`, and
`(770101, 1)` for the migration run row in `models/migration.py`. The second is
**derived** from the first, so moving one moves both. Session-level locks use
`django_pg_utils.advisory_lock` from `django-pgware`, which NetBox pins in its
own `requirements.txt` and takes its own locks through in `netbox/jobs.py`,
`extras/jobs.py` and `ipam/api/views.py`. Database-only project and declaration
writes use `project_write_lock()` on the same project key through
`pg_advisory_xact_lock`. That lock lasts until the enclosing transaction ends.

**The separation from core's locks is numeric, not a matter of arity.** It would
be easy to conclude otherwise, because PostgreSQL does keep the one-bigint and
two-integer keyspaces disjoint, but core uses both: `CustomField.data_lock_key()`
returns the pair `(ADVISORY_LOCK_KEYS['custom-field-data'], pk)`, which
`extras/jobs.py` takes at session scope through the same helper and
`extras/models/customfields.py` takes at transaction scope by hand. So the
guarantee rests only on the numbers. Every key core takes is either registered in
`ADVISORY_LOCK_KEYS` (`netbox/constants.py`, currently 100100 to 115100) or
hashed per tree by the `utilities/ltree.py` triggers, and none of them is 770100
or 770101.

`ADVISORY_LOCK_KEYS` is therefore the thing to watch. It is where a new core key
gets added, and `custom-field-data` shows core will use one as the namespace half of
a pair. The same script checks every entry against both namespaces, so one landing on
either value fails CI on the `feature` branch instead of waiting to be noticed in review.

The helper itself is a dependency the plugin relies on without declaring, as it
does with `django_rq` and `strawberry`. It arrives with NetBox.

## The ask upstream

The plugin has an open request for a documented generic execution context, which
would replace the execution rows above, bar one. In outline it should own the
transactions on both the default and the routed alias, apply the request
processors honouring commit intent, define what happens when one of them cannot
start, roll back a dry run without the caller raising an internal exception, clear
pending events when the body raises, and restore the request context on every
exit path including the one where the body failed.

Nothing in that list is specific to this plugin. It is what any plugin running
user-supplied code inside NetBox's transaction and event machinery needs.

The one execution row it would not replace is `AbortScript`, and that row needs
nothing from NetBox. It belongs to the built-in Custom Scripts feature, so it is
expected to go when that feature does at v5.0, and until then it is what a script
carried over unchanged raises to end a run. It needs no import guard either. On a
host above `max_version`, NetBox imports the plugin package to read its config,
warns that it cannot load the plugin, and skips it during settings load, so nothing
of the plugin beyond `__init__.py` and `constants.py` is ever imported there, and
neither reaches this symbol.

**There is a second motivation, and it is the more general one.** Without a
documented context, a plugin's only way to get change attribution and events is
to supply a request, because `core.signals.handle_changed_object` reads
`current_request` and returns before it records an ObjectChange or queues an
event. So a run with nobody at the other end, one an Event Rule drove or one
started from the command line, loses both unless it travels behind a synthesized
request. That one absence produces two separate symptoms, an unattributed change
and an event that never fires, and a context able to carry an attribution
identity without a synthetic request would close both.

Two narrower asks sit inside it, and both exist because a NetBox utility is almost
what the plugin needs. `utilities.request.apply_request_processors()` is the loop
`execution.py` writes out by hand, and the hand-written copy differs in two ways
the utility cannot express: it skips one named processor for a dry run, and it
treats a failure of `event_tracking` as fatal rather than as a warning, because a
committed run that silently emits nothing is worse than one that fails. Either a
parameter for both, or a documented way to compose the loop, would let the copy go.

`Job.enqueue()` accepts no `data`, so a caller cannot put anything on the row it
creates. With `immediate=True` it also runs the handler before returning, which
means there is no moment between the row existing and the script running in which
to write to it. A plugin that has to record what a run is pinned to therefore builds
the row itself. A `data` argument, or a documented hook that runs after the row is
saved and before the handler, would remove the one place this plugin duplicates a
core model's construction.

The migration rows have a request of their own: a legacy export service. It should
provide serializable records for the built-in script modules and their script
classes, each with its content checksum, its Data Source reference and the
repository path it was synchronized from, a stream for one module's stored source,
and the counts or records of the Event Rules, permissions, Jobs, schedules and
recurrences that name either model. That service would replace every read row above
attributed to `migration/source.py`, which is why they are confined to one module.

It would not replace the write rows, and it does not need to. Those are ordinary
writes to core models an operator could make by hand through NetBox's own UI, and
a migration only differs in doing them in the right order and recording what it
did. What makes them awkward is not that they are unsupported but that they reach
models being retired, so they carry the same field-name risk the read rows do.

The reason to ask rather than to keep reading the models directly is not capability.
A plugin can read all of it today, and this one does. It is that an installation's
migration should not depend on the field names of a feature being retired, and that
a supported export is the difference between a migration NetBox can guarantee and
one that happens to work.

A write fence is a separate ask, and it is narrower than this page used to claim. In
full it would be persistent, core-owned state that makes NetBox's own views, REST
viewsets, Event Rule dispatch and `runscript` refuse, below the view layer so there
is one place to enforce it rather than four.

**Most of the row-level half is buildable in a plugin today.** That was measured
rather than assumed. A `pre_save` receiver raising `AbortRequest` refuses a write to
`ScriptModule` or to `Script`, and it does so with no request in scope, so a
background job is refused as surely as a form post. NetBox documents the pattern for
plugins, though only for aborting a request, and core depends on the requestless case
itself: `handle_deleted_object` raises `AbortRequest` for a protection rule before it
looks for a request at all. Anyone building it should know that a direct save sends
`pre_save` with the **proxy** as sender while the automatic synchronization loads and
saves the **concrete** `ManagedFile`, so a fence has to connect to both or the
automatic path walks straight past it.

**Two things in the row-level half are out of reach, and they are what is left to
ask for.** The run paths named above are a separate matter: a signal cannot refuse an
execution, and once cleanup has deleted the rows there is nothing left to run.

The first is the synchronization write. `ManagedFile.sync_data()` writes the file
through the storage backend and never calls `save()`, so a row-level fence stops the
row and leaves the replacement content on the backend. There is no seam between the
two for a plugin to reach, and `SyncedDataMixin.clean()` calls `sync()`, so merely
validating the row is already enough to replace the content.

The second is core's own gap rather than a missing feature, and it is why the fence
cannot be extended to that path. The `auto_sync` receiver on `post_sync` iterates
every `AutoSyncRecord` on the source in a bare loop with no per-record error
handling, so any receiver that raises on one record ends the pass and every remaining
record is skipped in silence, including those belonging to config templates, export
templates, config contexts and config context profiles. The Data Source then reports
failed, because `sync()` writes `COMPLETED` before emitting the signal and the job
overwrites that status on the way out. **So the documented way to refuse a write
cannot be used on the synchronization path without breaking unrelated features.**
Per-record isolation there would fix that for every plugin, not only for this one.

**The cutover ships without it, and measuring what it could close is what made the ask
small.** The cutover withdraws every grant NetBox's own permissions UI can make,
disables every Event Rule that uses the feature, fails every queued run closed and
drops its task, and deregisters the source from synchronization. Cleanup then deletes
the Script and ScriptModule rows outright. Once those rows are gone there is nothing
left to execute, so the hole is not "the old scripts still run" but "a privileged user
can create new ones": a superuser, or anyone regranted `extras.add_scriptmodule`, can
still upload and run a fresh built-in script. Three narrower gaps sit beside it, and
all three are permission composition rather than the feature itself. A superuser
short-circuits every check. A permission named in `DEFAULT_PERMISSIONS` is merged in
from configuration rather than from a row. And a permission assigned as a plain Django
`auth_permission` is still honoured, because `RemoteUserBackend` extends `ModelBackend`
and sits ahead of NetBox's own backend.

Nothing on the inventory and staging side depends on the fence, because both passes are
re-runnable and content-addressed, so a source that moved underneath them simply
produces another revision. The cutover depends on it only for the guarantee, not for
correctness: it is what turns "plan a maintenance window" into "the feature is closed".
