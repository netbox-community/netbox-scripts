# Running Custom Scripts

A Custom Script is run from the Custom Scripts list or from its own page. The
plugin builds the run form out of the source the project is currently serving,
queues a background Job against a fixed revision of that source, and records the
run's log and output on the Job.

## Requesting a run

Choose **Run**, either from the row on the Custom Scripts list or from the
script's own page. The form is whatever the class declares: one field per
variable, grouped by the class's own fieldsets, plus the commit toggle.
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
| Scheduling and recurrence | The form carries no `_schedule_at`, `_interval` or notification fields yet, so every run is immediate |
| REST run endpoint | Runs are requested from the UI only |
| Declared pip requirements | A script's declared external dependencies are not checked before it runs |
| Event Rule action | A Custom Script cannot yet be the action of an Event Rule |
| Recorded input values | The Job records which script and revision ran, and the result, but not the values that were submitted |
| Configurable execution defaults | The timeout, notification policy and commit default are read from the class and cannot be overridden per installation |
