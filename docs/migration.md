# Migration

An installation that already uses NetBox's built-in Custom Scripts can have that content read,
reported on, staged as Script Projects, and finally handed over. This page covers the seven
passes that do it, how to read what they report, and what they deliberately leave alone.

The first two change nothing an operator depends on, and you can stop after them. The third is
irreversible. Read [Crossing the fence](#crossing-the-fence) before you run it.

## What the seven passes do

| Pass | What it does | Reversible |
|---|---|---|
| Inventory | Reads every built-in script module, classifies its authoring dialect, works out which Projects a migration would create, and counts the Event Rules, permissions and Jobs a migration would touch. Writes nothing. | Nothing to undo |
| Staging | Creates those Projects, declares their entrypoints, and stages their content as revisions. Activates nothing. | Yes, delete what it created |
| Cutover | Records every reference the repointing pass replays, then withdraws permissions on the built-in feature, disables its Event Rules, cancels its queued runs, and deregisters its source from synchronization. | **No** |
| Activation | Puts every staged Project into service, so its Custom Scripts exist as rows. | After the cutover |
| Repointing | Moves Event Rules, permissions and Job history onto those Custom Scripts, and recreates the schedules the cutover cancelled. | After the cutover |
| Cleanup | Deletes the built-in script modules this migration mapped, the Scripts under them, and their stored source. Records the migration as complete. | **No** |
| Verification | Reports whether the migration landed. Reads only, and is safe to run at any point and as often as you like. | Nothing to undo |

Each runs as a background job and records what it found on its own Job row, so the result stays
readable after the run. Run them in the order above. Each refuses if the one before it has not
completed. Verification is the exception: it waits for nothing and refuses nothing, so you can run it
between any two passes to see where the migration stands.

A migration is tracked as a single **migration run**, which holds the state, the journal the later
passes replay from, and what each pass recorded. Only one run is open at a time, and its state only
moves forward: `legacy`, `staging`, `cutover`, `migrated`.

## Before you start

Everything in this section is operator work. No pass can do any of it, and the cutover is
irreversible, so do all of it before you press **Enter cutover**.

1. **Take a maintenance window.** The fence a plugin can build withdraws every grant NetBox's own
   permissions UI can make, and no more. A superuser is unaffected. Treat the window as the real
   fence and this pass as the tidying.
2. **Pause the workers.** A run that starts while the cutover is capturing is a run the migration
   cannot account for. The cutover refuses outright while a built-in Script job is executing, so
   pausing first is what stops you having to wait mid-migration.
3. **Back up the database and the source storage together, as one restore point.** The plugin's
   Projects live in the database and their content lives in the storage backend, so a database
   restored against a different storage state serves revisions whose bytes are gone. Note the NetBox
   and plugin versions with the backup.
4. **Synchronize each Data Source one last time, and let its reconciliation finish.** Staging freezes
   whatever the Project holds at that moment, so a repository that moves afterwards leaves the
   migrated Project a revision behind. The **Reconcile Source** action on each Data Source Project is
   what drives that, and each Project's revision list is where you confirm it finished.
5. **Run the inventory and clear every blocking finding.** Staging refuses on any of them and creates
   nothing, so this is not optional. The Migration page lists them above the buttons.

## Starting a pass

*Custom Scripts > Migration* carries every pass and names the most recent run of each, so you can see
whether one is still queued and where the migration stands.

It also lists every Script Project the inventory and staging passes name, with the state each is in right
now. A Project the inventory proposed but staging has not created yet is listed as **Not staged**,
so running one pass without the other is visible rather than implied. Each state is read as the
page renders, so it is the verdict validation reached rather than what a pass recorded, and the
count beside it is how many Custom Scripts that revision publishes.

**Run inventory** queues the report. It changes nothing, so run it as often as you like.

Whatever the last inventory found that would refuse a migration is named on this page, one row per
module, with the reason and the code. Staging refuses on any of them and creates nothing, so the page
says so before you press the button rather than leaving you to read a failed Job. The
legacy-import list is counted rather than listed, because it is one entry per module and it does not
block anything. The inventory Job carries the full list.

**Stage Projects** confirms first, because it creates Script Projects. It refuses while
another staging pass is queued, and it refuses if the inventory reports any blocking finding. What it
reads is its own fresh report rather than the one on this page, so a blocking finding you have since
resolved does not stop it, and one introduced since the last inventory still will.

**Enter cutover** confirms first, and is the point of no return. The confirmation will not submit until you
state that the backup from step 3 of [Before you start](#before-you-start) is taken, because restoring it is
the only route back.

**Activate Projects** and **Repoint references** appear once the cutover has been recorded. Neither
confirms, because by then the decision has been made.

Every button returns you to this page, with the run it just queued named at the top. Follow that
link to the Job when you want the detail, because the log and the recorded result are both on the
Job's own page.

The inventory, staging and verification passes need permission to **add** a Script Project.
**Entering the cutover, activating, repointing and cleaning up need the Script Project
`migrate` action instead**, which is separate precisely because those four change rows this plugin
does not own and cannot be undone. A user holding `add` alone still sees the page and can run the
first two and the verification, and is not offered the other four. Reading any result needs the
*Core > Jobs* view permission, which is granted separately. See
[Permissions](permissions.md#the-migration-page).

## Reports are not covered

NetBox merged Reports into script modules in 4.0, so an installation upgraded from 3.x can still carry
module rows whose file root is `reports`. **No pass in this migration touches one.** They are excluded
from the inventory, from staging, and from the cutover's closures, which means a report keeps running
and keeps synchronizing exactly as it did.

The inventory reports how many it found and left, as a warning rather than a blocker, so the exclusion
is visible instead of silent. Reports use an authoring API this plugin does not serve at all, and its
own discovery refuses a report-style class outright, so there is nothing for a migration to move them
onto. Moving a Report means rewriting it as a Custom Script by hand.

## Reading the inventory

The report lands on the Job's data. It carries six keys.

| Key | What it holds |
|---|---|
| `status` | `ready`, `warning` or `blocking`, whichever is the worst level any finding reached. |
| `modules` | One entry per built-in script module: its path, its file root, its authoring dialect, and the script classes it publishes today. |
| `projects` | The Projects a migration would create, each with its key, name, source type and the modules it would hold. |
| `dialects` | How many modules fell into each dialect. |
| `references` | How many Event Rules, permissions and Jobs point at the built-in feature. |
| `findings` | Everything an operator has to act on, each with a level, a code, the module it belongs to and a message. |

The reference counts are the size of the cutover, not of this migration. Nothing here rewrites an
Event Rule, a permission or a Job.

## What the three statuses mean

| Status | Meaning |
|---|---|
| `ready` | Every module is already written against this plugin's authoring API. Staging can run. |
| `warning` | Staging can run, and there is work to do before NetBox v5.0. |
| `blocking` | Staging refuses. Something in the source could never be imported, or needs a rewrite. |

A `warning` is almost always the `legacy_import` finding: the module imports its authoring API from
`extras.scripts`. That import works here today and stops working at NetBox v5.0, so **the
legacy-import list is the work queue to clear before that upgrade**. It is what turns v5.0 into a
deadline rather than a cliff. See [Authoring](authoring.md) for the forms that resolve and for why
the compatibility layer is transitional.

The blocking findings are these.

| Code | Why it blocks |
|---|---|
| `report_style` | The class declares `test_` methods and no `run()`. A report needs a rewrite, at any NetBox version, so it is reported apart from the legacy-import list rather than inside it. |
| `not_importable` | The file name is not a valid Python identifier, so no loader could ever import it. A hyphenated name is the common case. Rename the file in the source. |
| `unparsable` | The stored source is not valid Python. |
| `source_unreadable` | The stored bytes could not be read at all. A legacy Report whose content sits outside the scripts storage root reports this. |

One unreadable or unparsable module never stops the inventory. It becomes a finding, and the rest
of the report is still produced.

## What grouping produces

A Project is one source tree and one Python package boundary, so a migration has to decide which
legacy modules belong together. The rule reads the path each module already records.

| Source | Result |
|---|---|
| Backed by a Data Source | One Project per folder that holds scripts, keyed by the Data Source and that folder. |
| Uploaded | One Project per module, holding that single file. |

A folder inside another script-holding folder joins the one above it rather than becoming a second
Project. A Project's tree already contains its subdirectories, so the higher Project holds the
deeper file either way, and two Projects would hold the same content twice.

One consequence is worth knowing, because it is a gain rather than a compromise. The built-in
feature stores a synchronized file under its base name and never brings a sibling with it, so a
script has no way to share code with a helper module. A migrated Project holds the **whole folder**,
so the helpers beside a script arrive with it and a relative import between them resolves.

## Staging

Staging creates each proposed Project and stages its content. It can be run as many times as you
like: identity is derived from the source rather than recorded in bookkeeping, so a Data Source
Project resolves to the one already covering that folder, an uploaded one to its own key, and
identical content resolves to the revision that already holds it. A second pass creates nothing.

Every Project it creates takes the **manual** activation policy, whatever you might choose for it
later. So staging settles nothing on its own: it queues each revision for validation, and the
verdict lands on the revision rather than on the pass. The job reports which revisions it queued,
not whether they are good. A revision that fails records what it found, which its own page lists.
Putting a valid one in service is the separate, deliberate step described under
[Putting a revision in service](data-sources.md#putting-a-revision-in-service).

A built-in module that publishes nothing migrates as a **helper file** rather than an entrypoint.
Its content is staged like any other file, but nothing declares it, so the Project it belongs to
never claims it publishes a Custom Script. A module counts as publishing when the built-in feature
recorded a Script for it, or when its source defines a class that could publish one, so source
already written against this plugin's API is declared even though the built-in feature never
recognised it.

The inventory names every module this applies to, because a genuine helper and a module that has
stopped importing look the same from the built-in rows. If one of them should be publishing a
Custom Script, fix it in the built-in feature and run the inventory again.

If the report is `blocking`, staging logs every blocking finding and stops without creating
anything. Fix the source, run the inventory again, and stage once it is clear.

## Crossing the fence

The cutover is the irreversible step, and it does two things in one pass: it records every
reference the later passes replay, and then it closes what a plugin is able to close.

**The run records the cutover state before it captures anything.** The state is the only thing
that can tell you the fence may have fired, so it is written before the first irreversible act
rather than after the last one. A cutover interrupted partway therefore reads `cutover` with the
crossing unrecorded, and three things follow: staging refuses that state outright, the Migration
page says so and keeps offering **Enter cutover**, and running it again finishes the crossing.
Nothing is captured twice and nothing is closed twice.

**It captures first.** Every permission granting an action on the built-in feature, with who holds
it. Every Event Rule naming the built-in feature, as an action or as a source. Every waiting
built-in Script job, with the input it was going to run with. All three go on the migration run's
journal, which is what makes the later passes replayable and what makes a half-finished migration
resumable rather than stuck. Capture happens once, because a second capture would read the closed
state back as though it were the original.

**Then it closes four doors.**

| What | How |
|---|---|
| Permissions | Every captured grant on the built-in feature is disabled. |
| Event Rules | Every captured rule is disabled, so nothing fires during the handover. |
| Queued runs | Every waiting job is failed closed and its task dropped, so nothing queued can still execute. The owner is notified, and the message says the plugin will recreate it. |
| Synchronization | The built-in script source is deregistered, so no later synchronization rewrites it. |

**What this is not.** It is not a complete write fence, and it does not try to be. It withdraws
every grant NetBox's own permissions UI can make and nothing more. A superuser still passes, and so
does anything `DEFAULT_PERMISSIONS` or a plain Django permission grant confers. Plan the cutover as
a maintenance window rather than relying on this alone.

**What it refuses.** A run that has not staged anything, a run that has already moved past the
cutover, and any installation where a built-in Script job is still running. Wait for those to
finish rather than cancelling them.

It also refuses while any Project this migration mapped could serve nothing on the far side, naming
each one and why. That covers a Project staging never created, one holding no revision, one whose
newest revision is still waiting on a verdict, and one whose newest revision was refused with no
earlier valid one behind it. A Project already serving a revision passes, including one serving an
older revision than its newest, because it keeps serving it across the fence.

The Migration page says the same thing and withholds the button, because a fence crossed with
nothing to serve leaves the built-in feature closed and the plugin publishing nothing, which only a
database and storage restore undoes. Wait for the verdicts that are still coming, and for the rest,
fix the source and stage it again. Each Project's revision list is where the verdict is recorded.

The check reads revision states and not stored content, so it does not catch a revision whose bytes
were removed from the storage backend after it validated. Activation reports that one and skips it.

Two warnings the pass can record rather than fail on. A queued job whose task is no longer in the
queue cannot have its input read, so it is named and left for you to recreate by hand. A job whose
input includes an uploaded file cannot have that value journalled, so it is recreated without it.

## Activating the staged Projects

**Activate Projects** puts every Project this migration staged into service. It comes after the
fence and before repointing, because a Custom Script row exists only once a revision is active, and
an Event Rule's action has to name one.

Per Project it takes the newest valid revision. A Project already serving its newest revision is
activated once more, which repairs its rows and writes nothing where nothing is wrong. A retired
revision is never chosen, because preferring it over an older valid one would serve something the
Project had already stood down from. A Project with no valid revision is reported and skipped, so
one bad Project does not stop the rest.

Safe to run again.

## Repointing what the installation refers to

**Repoint references** replays the journal onto the plugin's rows, in four steps.

Three of the four steps refuse while any migrated Project is serving no revision, because an Event
Rule and a repointed job both have to name a Custom Script that exists, and only a Project in
service publishes one. The Migration page withholds the button and names each Project. Put them
into service, run **Activate Projects** again, and the pass proceeds. Permissions is the exception
and runs regardless: it moves object types rather than scripts, and the cutover withdrew every
grant on the built-in feature, so holding it back would leave everyone but a superuser locked out
until an unrelated Project was fixed.

| Step | What moves |
|---|---|
| Event Rules | Each captured rule's action is pointed at the Custom Script that replaced its built-in Script, and the built-in object types it watched are replaced with the plugin's. A rule that moved completely is re-enabled. |
| Permissions | Each captured grant is moved onto the plugin's object types. An action with no counterpart on the plugin is dropped and reported. |
| Job history | The built-in Scripts' Jobs are moved onto the Custom Scripts that replaced them, so a run's history stays reachable from the script that replaced it. |
| Schedules | Every schedule the cutover cancelled is enqueued again against the Custom Script. |

Recreating a schedule follows five rules worth knowing, because between them they decide when a
migration runs your code and who it runs as.

- A schedule still in the future keeps its time.
- A recurrence that fell due during the handover keeps its interval and starts now. A queue runs a
  past-due job the moment it is enqueued, and a migration must not run a script unasked.
- A one-shot that fell due is refused for the same reason, and reported so you can decide.
- A schedule is replayed under the account that owned it, and only while that account can still run
  the script. One whose owner no longer holds the permission is refused and the step stays open, so
  granting it and running the pass again picks the schedule up.
- A schedule whose owner has been deleted or deactivated is refused for good rather than replayed
  with no owner or under an account that cannot act, because a recurring run nobody owns notifies
  nobody and is attributed to nobody, and no grant lets a deactivated account run anything.
  Recreate it by hand under an account that should own it.

**A run that was merely queued rather than scheduled is recreated to run at once**, with the commit
setting it was queued with, because that is what the cutover promised the owner when it cancelled it.
That is the one case where this pass executes your script, so if you would rather it did not, let the
queue drain before you enter the cutover. It refuses to start while a built-in Script job is actually
running, but a job still waiting is captured and replayed.

Each of the four steps records its completion only once it has left nothing a later run could still
do. So a reference it could not move, because the Custom Script it names does not resolve yet, is
picked up the next time you run the pass rather than skipped for good.

What it reports as **permanent** is not retried: it is what you would have to redo by hand rather
than fix and let the pass finish. A class removed or renamed before the migration keeps a built-in
Script row only so its history survives, and that row is deliberately absent from the map, so its
Job history, an Event Rule naming it and a schedule naming it are all left where they are for good.
So are a job naming a built-in module rather than a Script, a permission carrying constraints, and
a schedule whose time has passed, whose owner has been deleted or deactivated, or whose input names
an object you deleted. Every one of those is listed on the migration's own page so you can deal
with it by hand, and none of them holds the migration open.

**Staying put is not the same as staying reachable.** NetBox removes the built-in Custom Scripts
feature at v5.0. It says nothing about deleting the rows, but a row naming a model that no longer
exists has no page to open it on, so anything permanent whose history matters to you is worth
dealing with before that upgrade rather than after.

## Retiring the built-in rows

Cleanup is the last pass and the only one that deletes anything. It refuses until every part of the
repointing pass has finished, because deleting a built-in Script deletes its Job rows with it and a
captured schedule can only be recreated while the built-in rows are still there.

It deletes only the modules this migration mapped, one at a time, and the stored source of each goes
with it. That is safe only because staging copied every byte into this plugin's own storage first, so
check that each migrated Project serves a revision before you run it. Any module the migration did not
map is left alone.

**A module that something still refers to is left in place and named in the job log**, because every
one of those references would be deleted along with it rather than orphaned. The log distinguishes
two kinds, and the difference decides whether the migration can finish.

**Retained, which no action of yours clears.** These stay for good and do not hold the migration
open:

- The module holds Job rows of its own. Older NetBox versions recorded a run against the module
  rather than against the Script, and a Script Project cannot hold jobs.
- The module holds Job history for a class that has since left the file. NetBox keeps such a Script
  row, not executable, purely for its history, and nothing in this plugin replaces it. This is
  ordinary on a long-lived installation.

**Blocked, which you can clear.** These hold the migration open until you deal with them:

- A live Script under the module still holds Job rows the repointing pass did not move. Run that
  pass again, then retry cleanup.
- An Event Rule still names the module, which means the repointing pass has not run or did not
  finish.
- The module publishes a class no Custom Script resolves to. Deleting it would leave a script that
  used to run unable to run at all, so fix the source and stage it again first.
- The Project replacing the module is not serving a revision. Activate it, then retry.

The migration reaches the `migrated` state once nothing **blocked** is left. A retained module does
not keep it open, because nothing would ever clear it and a run that cannot close is a run no
replacement can be started for. **Verify** names every retained module on each run, so the residue
stays visible rather than forgotten.

## After the last pass

Two more pieces of operator work, in this order.

1. **Restart every web and worker process.** A process that imported a built-in script module still
   holds it in memory, and deleting the row does not unload it. Until every process has restarted,
   what an installation can execute is not what its rows say.
2. **Resume the queues.**

Then run **Verify**, and read what it reports before you call the migration done.

### Two guarantees, and what each one is worth

**Disabling this plugin does not bring the built-in Custom Scripts back.** Every door the migration
closed is a row in a NetBox table rather than a decision this plugin re-makes at runtime: a deleted
script module, a disabled Object Permission, a deregistered synchronization record. Those rows read
the same whether the plugin is installed, disabled or removed. The honest limit is that the same
thing makes it reversible by hand. Anyone who can create an Object Permission can grant
`extras.add_scriptmodule` again and upload a new built-in script, and nothing this plugin ships can
refuse that. It is a fresh script rather than a returning one, because the old rows are gone.

**A stale built-in job cannot execute.** The cutover fails every waiting job closed and drops its
task from the queue, so there is nothing left for a worker to pick up. Recurring runs are recreated
against the Custom Script that replaced the built-in one, subject to the owner rules above, which is
why the reference pass comes before cleanup rather than after.

## Recovery

**Before the fence, recovery is abandonment.** Delete the staged Projects and their revisions. The
built-in feature has not been touched, so there is nothing to undo and no state to reconcile.

**After the fence there is no rollback.**

**A run reading `cutover` has not necessarily finished crossing**, so treat the backup as still
required for any run in that state. The Migration page names it and keeps **Enter cutover**
available, and that is the pass to run. The recorded crossing, shown on the run's own page, is what
says the fence is fully closed.

That is a property of the design rather than a missing feature. The cutover deletes queue tasks,
disables rows an operator may since have edited, and hands execution to Projects whose content is
addressed by digest. Nothing reconstructs the state before it.

The supported reversal is restoring the backup from step 3 of
[Before you start](#before-you-start): the database, the source storage, and the matching NetBox and
plugin versions, together. Restoring one without the others gives an installation that disagrees with
itself.

Forward is the cheaper direction in every case the migration leaves unfinished. A Project serving no
revision is activated, a permission left withdrawn is recreated by hand, a module cleanup skipped is
retired once its Job history is dealt with. **Verify** names each of those every time it runs.

## Checking whether it landed

**Verify** runs five checks and changes nothing. Each one reports `ready`, `warning` or `blocking`,
the report takes the worst of them as its status, and the whole thing is recorded on the Job so it
stays readable.

| Check | Passes when |
|---|---|
| Modules | Every Project this migration activated exists and serves a revision |
| Scripts | Every built-in Script has a live Custom Script that is not retired |
| Event Rules | No Event Rule names the built-in feature, and every rule that was enabled before the cutover is enabled again |
| Permissions | No permission names the built-in feature |
| Jobs | No Job names the built-in feature, every captured schedule has a live counterpart, and one still waiting holds a task in the queue |

**A check reports `warning` until the pass it verifies has run**, so a report taken before the
cutover says that nothing has happened rather than that something is wrong. Only a pass that has run
and left something behind reports `blocking`.

**Every check names what it read.** That matters after cleanup, because the built-in rows are gone by
then and the Scripts check has to fall back to what the migrated Projects publish. A report that says
`the built-in rows` was checked against them directly, and one that says `the migration journal` was
checked against what the migration recorded. Without that, a green report after cleanup would be
indistinguishable from a check that had nothing left to look at.

**One Jobs result is `blocking` rather than a warning.** A schedule the journal records as migrated
whose Job row is still waiting with no task in the queue will never fire, and no re-run of the
reference pass fixes it, because the journal already claims it. Recreate that schedule by hand.

**A queue this pass cannot reach is reported as a warning, not as a loss.** The Jobs check then says
so and names the journal alone as what it read, because an unread queue cannot tell a missing task
from an unreachable one. Run **Verify** again once the queue is back.

Two other `warning` results are ordinary rather than faults, and both are restated on every run
because each is operator work that stays outstanding until somebody does it: a permission that
carried constraints and was left withdrawn for you to recreate, and a Job that names a built-in
script module rather than a Script, which no Script Project can hold.

## What is not part of this release

| Area | Status |
|---|---|
| A complete write fence | Not in this release. The row-level half is buildable in a plugin and deliberately not shipped, and the rest needs NetBox. See [Crossing the fence](#crossing-the-fence). |
| Choosing a different grouping | Not planned. Edit the staged Projects afterwards if you want a different shape. |
| Reversing a cutover | Not planned. See [Recovery](#recovery). |
