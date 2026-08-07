# Running Custom Scripts

A Custom Script is run from the Custom Scripts list or from its own page. The
plugin builds the run form out of the source the project is currently serving,
queues a background Job against a fixed revision of that source, and records the
run's log and output on the Job.

## Requesting a run

Choose **Run**, either from the row on the Custom Scripts list or from the
script's own page. The form is whatever the class declares: one field per
variable, grouped by the class's own fieldsets, plus the execution parameters.
Submitting queues a Job and takes you to its result page.

The button is present but inert when the script cannot be run, in both places.
That happens when the script is disabled or retired, when its project is
disabled, or when the project is not serving a revision at all.

A script's page also shows the execution defaults its author declared: whether
commit starts on, the run timeout, who is notified, and whether the script may
be scheduled. They are read from the class on every activation, so they are
reported rather than configured.

Running is its own permission, `run`, granted separately from `change`. Someone
who may edit a script's administrative fields cannot necessarily run it, and the
reverse holds too. Grant it like any other action, by ticking **run** on an
Object Permission for Custom Scripts.

## Running over REST

`POST /api/plugins/custom-scripts/scripts/<id>/run/` requests a run without a
browser. The body is the same shape the built-in script endpoint accepts, so a
caller moving over changes the URL and nothing else.

```json
{
  "data": {"site": 3, "count": 5},
  "commit": true,
  "schedule_at": "2026-08-11T02:00:00Z",
  "interval": 1440,
  "notifications": "on_failure"
}
```

Every field is optional. The variable values go in `data`, which keeps a
variable named `commit` or `interval` from colliding with an execution
parameter. An omitted `commit` or `notifications` takes the default the script
class declared.

The values in `data` are validated by the same form the run page renders, so a
bad value comes back as a 400 naming the variable that was wrong. The reply to
an accepted run is the Job itself, at 201:

```json
{
  "id": 88,
  "url": "/api/core/jobs/88/",
  "status": {"value": "scheduled", "label": "Scheduled"},
  "scheduled": "2026-08-11T02:00:00Z"
}
```

Poll that URL for the outcome. The `run` permission is what this route
requires, not `add` or `change`.

Two refusals are worth knowing about. A run is refused with 503 when no worker
is running, because a queued run nothing can pick up gives no signal that it
will never start. And a script whose author set `scheduling_enabled = False`
refuses `schedule_at` and `interval` with a 400 rather than ignoring them, since
there is no form here to leave them out of.

## Scheduling a run

Four execution parameters sit below the script's own fields.

| Field | What it does |
|---|---|
| **Commit changes** | Whether the run's database changes are kept. Default is the class's `commit_default`. |
| **Schedule at** | Run once, at a time in the future. Leave empty to run now. |
| **Recurs every** | Run repeatedly, in minutes. The picker offers the usual intervals and any whole number is accepted. |
| **Notifications** | When to notify you about the Job. Default is the class's `notifications_default`. |

A time in the past is refused. Setting a recurrence with no start time begins it
now. A script whose author set `scheduling_enabled = False` shows neither
scheduling field, because that flag is a statement that the script is not safe to
run unattended. Notifications stay available either way, since they describe the
run rather than the schedule.

## Revision pinning

The revision is fixed at the moment the run is requested, not at the moment a
worker picks the Job up.

That matters because a project's source can change between the two. If someone
uploads new source, or a Data Source sync activates a newer revision while your
Job is still queued, your run still executes the source you were looking at when
you filled the form in. The Job records which revision it used, and the result
page names it.

A run is not cancelled by deactivating the project's revision afterwards, for the
same reason. It is stopped by disabling the script or its project, because
`enabled` is an administrative control and it is rechecked when the worker starts.

### A recurring run is not pinned

A recurrence resolves the project's active revision at **each** occurrence, and
records on the Job which one it used.

That is the opposite of a one-shot run, and deliberately so. A one-shot run is
pinned because there was a moment when somebody read the source and asked for it.
A recurrence has no such moment after the first, so pinning would mean a nightly
job still executing the source that was active the day it was created, silently,
however many times the project was updated since.

The consequence to know about: an occurrence whose project is serving no revision
fails rather than falling back to what ran last. Reactivating a revision makes the
next occurrence work again, with no need to recreate the schedule.

## Commit and dry run

The commit toggle decides whether the run's database changes are kept.

With commit **on**, changes are written normally. Change logging records them
against the user who requested the run, and Event Rules and webhooks fire as they
would for any other change.

With commit **off**, everything the script writes is rolled back when it finishes.
The script still receives `commit=False`, so it can behave differently if the
author wrote it that way, and the run still records its full log and its output.
No events are queued, so a dry run cannot trigger a webhook by accident.

A script that raises is treated the same way as a dry run as far as the database
is concerned: everything it wrote is rolled back, and pending events are
discarded. The difference is that the run is recorded as failed and the log
carries the exception and its traceback. A script that calls `AbortScript` stops
cleanly, and its message is recorded without a traceback, because the author
already said what went wrong.

## Reading a result

The result page shows the Job's status, who requested it, the revision it ran,
the run log, and whatever the script returned.

The log is filtered to info and above by default. Add `?log_threshold=debug` to
the URL to see debug records as well, or `?log_threshold=warning` to see only
problems.

Every run a script has ever performed is on its **Jobs** tab. Because a script
that stops being published is retired rather than deleted, that history survives
a revision that drops the class and comes back if a later revision publishes it
again.

## What a run does not see

Each run imports the revision fresh and unloads it afterwards, so module-level
state does not carry from one run into the next. A script that caches something in
a module global will find it gone on the next run, whichever worker picks it up.

Source is verified against the revision's manifest before any of it is imported,
every time. A cached copy is never trusted because it exists.

Nothing about the storage layout reaches the run record. Storage keys, content
digests and local cache paths are stripped out of the log before it is saved,
including out of a traceback, which names the file it was raised in.

## Not implemented yet

These are known gaps rather than design decisions, each tracked against a later
piece of work.

| Gap | Notes |
|---|---|
| Declared pip requirements | A script's declared external dependencies are not checked before it runs |
| Event Rule action | A Custom Script cannot yet be the action of an Event Rule |
| Recorded input values | The Job records which script and revision ran, and the result, but not the values that were submitted |
| Configurable execution defaults | The timeout, notification policy and commit default are read from the class and cannot be overridden per installation |
