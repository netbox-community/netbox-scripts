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

Three modules, on purpose. If NetBox adds a documented execution context, replacing
the execution rows is a change to `execution.py` alone. The three `api/views.py`
rows are the REST run endpoint's, and none of them touch how a run executes.

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
