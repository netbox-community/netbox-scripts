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
| `django.db.router.db_for_write` on a change-logged core model | `execution.py` | Which database change-logged writes go to, which is a branch schema while a branch is active |
| `utilities.request.copy_safe_request` | `views/script.py`, `api/views.py` | A picklable, sensitive-header-stripped copy of the request, so it can travel to a worker |
| `utilities.rqworker.any_workers_for_queue` | `api/views.py` | Whether a worker is live for the queue, so a REST run that nothing could pick up is refused rather than queued |
| `utilities.exceptions.RQWorkerNotRunningException` | `api/views.py` | The 503 that refusal answers with, which is what NetBox's own run endpoint returns |
| `netbox.api.authentication.TokenPermissions` | `api/views.py` | The permission class the REST run action subclasses, so a POST resolves to the run permission rather than to add |
| `extras.models.ScriptModule` and its manager | `migration/source.py` | Every built-in script module. A proxy on `ManagedFile` whose manager admits both the `scripts` and `reports` roots, so legacy Reports are already rows |
| `extras.models.Script` through `module.scripts` | `migration/source.py` | Which script classes each built-in module publishes today |
| `core.models.ManagedFile` fields `file_root`, `file_path`, `data_path`, `data_source` | `migration/source.py` | Where a module's bytes are and which repository path it came from. NetBox marks this model `_netbox_private` |
| `extras.models.mixins.PythonModuleMixin.python_name` | `migration/source.py` | The module name the built-in loader imports a file under |
| `storages['scripts']` | `migration/source.py` | The stored bytes of one built-in module, opened by `file_path` because that is what the built-in loader opens |
| `extras.models.EventRule.action_object_type` | `migration/source.py` | How many Event Rules a migration would have to repoint |
| `users.models.ObjectPermission.object_types` | `migration/source.py` | How many permissions name the built-in models |
| `core.models.Job.object_type` | `migration/source.py` | How much Job history exists, including what is scheduled or recurring |
| `extras.models.ScriptModule.jobs` and `.event_rules` | `migration/source.py` | Whether deleting one module would take Job history or an Event Rule with it. Both are `GenericRelation`s, so Django's collector deletes those rows rather than orphaning them |
| `users.models.ObjectPermission.enabled` | `migration/cutover.py` | Withdrawing every grant on the built-in feature, which is as close to a write fence as a plugin gets |
| `extras.models.EventRule.enabled` | `migration/cutover.py`, `migration/references.py` | Taking a rule out of service for the handover, and putting it back once its action and sources name plugin rows |
| `core.models.Job.terminate` | `migration/cutover.py` | Failing a queued run closed. Used rather than an `update()` so the owner is notified, and there is no cancelled status to set |
| `django_rq.get_queue` and `rq.job.Job.fetch` / `.delete` | `migration/cutover.py` | A queued run's input, which lives only on the RQ task because `Job.enqueue()` keeps it off the row, and then dropping that task so nothing can execute it |
| `core.models.AutoSyncRecord` | `migration/cutover.py` | Deregistering the built-in source, so no later synchronization rewrites it. Filtered on the **concrete** `ManagedFile` type, the inverse of the proxy rule below |
| `extras.models.EventRule.action_type`, `.action_object_type`, `.action_object_id`, `.object_types` | `migration/references.py` | Repointing a rule onto the Custom Script that replaced its built-in one. `full_clean()` first, because a rule can be invalid for reasons that predate the migration |
| `users.models.ObjectPermission` creation, `.actions`, `.object_types`, `.users`, `.groups` | `migration/references.py` | Moving a grant onto the plugin's models, and splitting one that also named something else |
| `core.models.Job.object_type` / `.object_id` update | `migration/references.py` | Repointing run history, batched, and done before anything is deleted because a Script's jobs go with it |
| `extras.models.ScriptModule.delete` | `migration/cleanup.py` | Retiring a module and its stored source. Called per instance, because `QuerySet.delete()` does not call the model's `delete()`, which is what removes the file |

Six modules, on purpose. If NetBox adds a documented execution context, replacing
the execution rows is a change to `execution.py` alone. The three `api/views.py`
rows are the REST run endpoint's, and none of them touch how a run executes.

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

## Why they are not avoidable

The behaviour they provide is not optional. Without the request processors a
script's database changes get no user attribution and its events never reach the
event pipeline, which silently breaks change logging, webhooks and Event Rules for
everything the script touches. Without the router probe a script running inside a
branch writes to the wrong schema. Without a safe request copy the run cannot
reach a worker at all.

The `AbortTransaction` NetBox uses for dry-run rollback is the one internal we
declined to depend on. It is a bare exception used purely as a rollback trigger,
so the plugin defines its own equivalent and behaves identically.

## The ask upstream

The plugin has an open request for a documented generic execution context, which
would replace every row above. In outline it should own the transactions on both
the default and the routed alias, apply the request processors honouring commit
intent, define what happens when one of them cannot start, roll back a dry run
without the caller raising an internal exception, clear pending events when the
body raises, and restore the request context on every exit path including the one
where the body failed.

Nothing in that list is specific to Custom Scripts. It is what any plugin running
user-supplied code inside NetBox's transaction and event machinery needs.

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

A write fence is a separate ask, and the only one this plugin could not build. It is
persistent, core-owned state that makes NetBox's own views, REST viewsets, Event Rule
dispatch and `runscript` refuse, below the view layer so there is one place to enforce
it rather than four. No plugin can do that without monkey-patching.

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
