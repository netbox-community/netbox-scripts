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

Four modules, on purpose. If NetBox adds a documented execution context, replacing
the execution rows is a change to `execution.py` alone. The three `api/views.py`
rows are the REST run endpoint's, and none of them touch how a run executes. Every
migration row is confined to `migration/source.py`, which is the only module in the
plugin that reads the built-in implementation at all.

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
recurrences that name either model. That service would replace every row above
attributed to `migration/source.py`, which is why they are confined to one module.

The reason to ask rather than to keep reading the models directly is not capability.
A plugin can read all of it today, and this one does. It is that an installation's
migration should not depend on the field names of a feature being retired, and that
a supported export is the difference between a migration NetBox can guarantee and
one that happens to work.

A write fence is a separate ask, and a harder one: something that holds the built-in
feature still for the duration of a cutover, so source cannot change underneath a
migration in progress. No plugin can implement that without monkey-patching. Nothing
on the inventory and staging side depends on it, because both passes are re-runnable
and content-addressed, so a source that moved underneath them simply produces another
revision. The fence is the cutover's requirement, not theirs.
