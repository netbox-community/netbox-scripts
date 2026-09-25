# Running Scripts

Run a Script from the Scripts list or its detail page. The plugin builds the
form from the source the Project currently serves, queues a background Job,
and records its log and output.

## Requesting a run

Choose **Run**, fill in the Script's variables and execution settings, and
submit the form. The variables use the Script's fieldsets when defined.
Submitting opens the queued Job's result page.

The **Run** button is disabled when the Script is disabled or retired, its
Project is disabled, or the Project has no active revision.

The Script's page shows its effective commit default, timeout, notification
policy and scheduling setting. Operators can override the first three. See
[Overriding a script's execution defaults](#overriding-a-scripts-execution-defaults).

Running requires `run` permission, separately from `change`. Permission to edit
a Script does not grant permission to run it, or vice versa. Scheduling also
requires `schedule`. See [Permissions](../administration/permissions.md).

### Reaching a script you run often

Bookmark frequently used Scripts to find them on your NetBox dashboard. You
can also bookmark Projects and Script Files, but not revisions.

Bookmarks are personal to your account. They reference the Script row, so
retirement does not remove them. If a later activation republishes the class,
you can use the same bookmark to run it again.

## Running over REST

Request a run with `POST /api/plugins/netbox-scripts/scripts/<id>/run/`.
The endpoint uses the built-in Script endpoint's request-body structure:

```json
{
  "data": {"site": 3, "count": 5},
  "commit": true,
  "notifications": "on_failure"
}
```

The top-level fields are optional. Put the Script's variable values in `data`,
where its form requirements still apply. This separates variables named
`commit` or `interval` from execution parameters.

A Script with a `FileVar` needs a multipart request, because a file
cannot travel inside JSON. Send `data` as a JSON string and each file
as a part named after its variable:

```bash
curl -sS -X POST \
  -H "Authorization: Token $NETBOX_TOKEN" \
  -F 'data={"site": 3}' \
  -F inventory=@devices.csv \
  https://netbox.example.com/api/plugins/netbox-scripts/scripts/12/run/
```

Other top-level fields, such as `commit`, go in their own parts.

Omitting `commit` or `notifications` uses the Script's effective default: an
operator override when set, otherwise the class default. See
[Overriding a script's execution defaults](#overriding-a-scripts-execution-defaults).

To schedule a run, add a future `schedule_at` timestamp and an `interval` when
it should repeat. Past timestamps are rejected. Include an explicit timezone.
A timestamp without one is interpreted in NetBox's configured timezone.
See [Scheduling a run](#scheduling-a-run).

The endpoint validates `data` through the same form as the run page. Invalid
values return HTTP 400 with the affected variable named. An accepted request
returns the Job with HTTP 201:

```json
{
  "id": 88,
  "url": "/api/core/jobs/88/",
  "status": {"value": "pending", "label": "Pending"}
}
```

Poll the returned URL for the result. This route requires `run` permission,
not `add` or `change`.

The request returns HTTP 503 when no worker is running. Supplying `schedule_at`
or `interval` returns HTTP 400 when the Script has `scheduling_enabled = False`,
or HTTP 403 when you lack `schedule` permission. Unsupported scheduling
parameters are rejected rather than ignored.

## Replacing `runscript`

NetBox removes the built-in `runscript` command along with its Custom Scripts
implementation. Use `runcustomscript` from the NetBox host, or the REST endpoint
from other callers.

### From a shell

`manage.py runcustomscript` runs a Script in the calling process and waits for
it to finish. Use it for a cron entry or a CI step with access to the NetBox host:

```bash
python netbox/manage.py runcustomscript deploy.MakeTag \
    --commit --data '{"site": 3}' --loglevel warning
```

The command has a separate name because NetBox's own `runscript` takes precedence
while both are installed. Its arguments correspond to the built-in command's:

| `runscript` | Here |
|---|---|
| `script`, as `module.ClassName` | Use `project:module.ClassName`. Omit the Project when the name identifies one Script. Ambiguous names are rejected with the candidates listed. |
| `--commit` | Same option. |
| `--data '<json>'` | Same option. Values are validated through the Script's form, with invalid variables named in the error. |
| `--user <name>` | Unknown names are rejected instead of falling back to the first superuser. Omitting the option still uses the first superuser. |
| `--loglevel <level>` | Uses the plugin's levels: `debug`, `info`, `success`, `warning` and `failure`, rather than the built-in command's `error` and `critical`. |

The command also accepts `--notifications`, which has no built-in equivalent.

**The command exits non-zero unless the Job completes.** A Script that raises
therefore fails a CI step rather than returning success. Calling `log_failure()`
and returning normally still completes the Job. Raise `AbortScript` when the run
should fail. Failures before Script execution are read from the Job log and
written to standard error.

**`job_timeout` is not enforced by this command.** It runs without an RQ worker,
so a runaway Script must be stopped separately. The Job row is committed before
execution, making the run visible and leaving a record if it is interrupted.

This command is for self-hosted deployments. NetBox Cloud and NetBox Enterprise
cannot invoke management commands. Use REST there.

### From anything else

Use the REST endpoint when the caller cannot run a command on the NetBox host,
including on NetBox Cloud and Enterprise. It queues the run and returns HTTP 201
with the Job. Poll `/api/core/jobs/<id>/` until the Job reaches a terminal status.

## Scheduling a run

The run form places execution settings below the Script's variables.

| Field | What it does |
|---|---|
| **Commit changes** | Keep the run's database changes. Starts with the Script's effective `commit_default`. |
| **Schedule at** | Run at a future time. Leave empty to run now. |
| **Recurs every** | Repeat at an interval in minutes. Choose a listed interval or enter a whole number. |
| **Notifications** | Choose **Follow the Script** to use its effective `notifications_default`, or select a policy for this run. |

A past time is rejected. Recurring runs cannot carry uploaded files. See
[Variables](authoring.md#variables). A recurrence without a start time begins
now. Notifications are available for both immediate and scheduled runs.

**Follow the Script** preserves inheritance, not just the policy shown in its
label. Each new recurring Job uses the policy in effect when that occurrence
is queued. Jobs already queued keep their policy. Selecting a specific policy
keeps it for the recurrence until the schedule is recreated.

Effective defaults include operator overrides. See
[Overriding a script's execution defaults](#overriding-a-scripts-execution-defaults).

The scheduling fields appear only when the author permits scheduling and you
have `schedule` permission.

## Overriding a script's execution defaults

With the Script's `change` permission, you can override its commit default,
timeout and notification policy on the edit page, through bulk edit or over
REST. These settings take precedence over the defaults in the Script's `Meta`
class.

| Setting | Overridable | Resolved as |
|---|---|---|
| Commit by default | Yes | Override, then class `commit_default`, then on. |
| Run timeout | Yes | Override, then class `job_timeout`, then system setting. |
| Notifications | Yes | Override, then class `notifications_default`, then Always. |
| Scheduling allowed | No | Class `scheduling_enabled` only. |

Invalid recorded defaults or overrides reject the run before a Job is created,
with an error naming the setting. If this affects a recurring Script, later
occurrences are not queued and NetBox records the reason on the last Job.

Scheduling remains the author's decision. `scheduling_enabled` cannot be
overridden by an operator.

Overrides survive activation. Activation refreshes the class-derived display
name, description and metadata, along with retirement status. It preserves
`enabled`, execution overrides and other operator-maintained fields.

Clear an override to inherit the class value again. An empty timeout override
also means inheritance, so it cannot bypass a class timeout and select the system
default instead. Set an explicit timeout when you need a different value.

## Revision pinning

A one-shot run is pinned to the revision active when it is requested. If newer
source activates while the Job waits, the queued run still uses its pinned
revision. The Job and result page identify that revision.

Deactivating the revision does not cancel a pinned run. Disabling the Script or
its Project prevents execution because the worker checks `enabled` when it starts.

### A recurring run is not pinned

Each recurring occurrence resolves the Project's active revision and records
which revision it used. A schedule therefore follows source updates rather than
remaining tied to the revision active when the schedule was created.

An occurrence fails if the Project has no active revision. It does not fall back
to the last source it ran. Reactivating a revision allows the next occurrence
to run without recreating the schedule.

## Commit and dry run

The commit setting controls whether the run's database changes are kept.

With commit enabled, changes are kept, attributed to the requesting user, and
processed through NetBox's change logging and object-change events.

A run always writes to the main schema, even when a NetBox Branching branch is
active. See [NetBox Branching](../administration/branching.md#script-execution).

With commit disabled, database changes are rolled back when the run finishes.
The Script receives `commit=False` and can use it to adjust its behavior. Its
log and output are still recorded. The run's queued object-change events are
not published. Job start and completion events are separate. See
[Event Rules](../administration/event-rules.md#scripts-as-event-sources).

**A dry run does not undo external actions.** Device configuration, HTTP requests
and file writes still take effect. Check `commit` before performing them.

If a Script raises an exception, its database changes are rolled back and pending
object-change events are discarded. The Job fails and records the exception
and traceback. Raise `AbortScript` to fail with an explanatory message without
a traceback.

## Reading a result

The result page shows the Job's status, requesting user, revision, log and output.
The log shows info and higher levels by default. Add `?log_threshold=debug` to
include debug records, or `?log_threshold=warning` to show warning and failure
records.

The Script's **Jobs** tab shows its history for as long as NetBox retains those
Job records. Retiring a Script does not delete it or its history. A later
activation can republish the same Script.

## What a run does not see

Each run imports its revision afresh and unloads it afterwards. Module-level
state does not persist between runs, regardless of which worker executes them.

Source is checked against the revision manifest before every import. An existing
cache entry is verified too.

The plugin removes its storage keys, content digests and cache paths from log
messages, tracebacks and string output before saving them. The Job retains its
revision digest to identify the source that ran.

## What is not in this release

| Gap | Notes |
|---|---|
| Declared pip requirements | A Script's declared external dependencies are not checked before execution. |
| Recorded input values | The Job records the Script, revision and result, but not the submitted values. |
