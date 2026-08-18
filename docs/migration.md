# Migration

An installation that already uses NetBox's built-in Custom Scripts can have that content read,
reported on, staged as Custom Script Projects, and finally handed over. This page covers the seven
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

## Starting a pass

*Custom Scripts > Migration* carries both passes and names the most recent run of each, so you
can see whether one is still queued.

It also lists every Custom Script Project the inventory and staging passes name, with the state each is in right
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

**Stage Projects** confirms first, because it creates Custom Script Projects. It refuses while
another staging pass is queued, and it refuses if the inventory reports any blocking finding. What it
reads is its own fresh report rather than the one on this page, so a blocking finding you have since
resolved does not stop it, and one introduced since the last inventory still will.

**Enter cutover** confirms first, and is the point of no return.

**Activate Projects** and **Repoint references** appear once the cutover has been recorded. Neither
confirms, because by then the decision has been made.

Every button returns you to this page, with the run it just queued named at the top. Follow that
link to the Job when you want the detail, because the log and the recorded result are both on the
Job's own page.

Starting any pass needs permission to add a Custom Script Project, and reading the result needs
the *Core > Jobs* view permission, which is granted separately.

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

If the report is `blocking`, staging logs every blocking finding and stops without creating
anything. Fix the source, run the inventory again, and stage once it is clear.

## Crossing the fence

The cutover is the irreversible step, and it does two things in one pass: it records every
reference the later passes replay, and then it closes what a plugin is able to close.

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

**What this is not.** It is the closest thing to a write fence a plugin can build, and it is not a
complete one. It withdraws every grant NetBox's own permissions UI can make and nothing more. A
superuser still passes, and so does anything `DEFAULT_PERMISSIONS` or a plain Django permission
grant confers. Plan the cutover as a maintenance window rather than relying on this alone.

**What it refuses.** A run that has not staged anything, a run that has already moved past the
cutover, and any installation where a built-in Script job is still running. Wait for those to
finish rather than cancelling them.

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

| Step | What moves |
|---|---|
| Event Rules | Each captured rule's action is pointed at the Custom Script that replaced its built-in Script, and the built-in object types it watched are replaced with the plugin's. A rule that moved completely is re-enabled. |
| Permissions | Each captured grant is moved onto the plugin's object types. An action with no counterpart on the plugin is dropped and reported. |
| Job history | The built-in Scripts' Jobs are moved onto the Custom Scripts that replaced them, so a run's history survives the migration. |
| Schedules | Every schedule the cutover cancelled is enqueued again against the Custom Script. |

Recreating a schedule follows one rule worth knowing. A schedule still in the future keeps its
time. A recurrence that fell due during the handover keeps its interval and starts now, because a
queue runs a past-due job the moment it is enqueued and a migration must not run an operator's
script unasked. A one-shot that fell due is refused for the same reason, and reported so you can
decide.

Pointing an Event Rule's **action** at a Custom Script needs NetBox 4.7, where the plugin action
exists. Below that line the rule's sources still move and its action is reported as unmoved.

## Retiring the built-in rows

Cleanup is the last pass and the only one that deletes anything. It refuses until the repointing
pass has moved the Job history, because deleting a built-in Script deletes its Job rows with it.

It deletes only the modules this migration mapped, one at a time, and the stored source of each goes
with it. That is safe only because staging copied every byte into this plugin's own storage first, so
check that each migrated Project serves a revision before you run it. Reports, and any module the
migration did not map, are left alone.

**A module whose Job history has not moved is left in place and named in the job log.** Two cases
reach that, and both are history a deletion would destroy rather than orphan:

- A built-in Script under the module still holds Job rows, because no Custom Script resolved to it.
- The module holds Job rows of its own. Older NetBox versions recorded a run against the module
  rather than against the Script, and a Custom Script Project cannot hold jobs, so the repointing
  pass leaves those where they are.

Clear what each warning names, then run cleanup again. The migration reaches the `migrated` state
only once nothing was left behind, so a partial pass stays resumable rather than closing the run.

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
| Jobs | No Job names the built-in feature, and every captured schedule has a live counterpart |

**A check reports `warning` until the pass it verifies has run**, so a report taken before the
cutover says that nothing has happened rather than that something is wrong. Only a pass that has run
and left something behind reports `blocking`.

**Every check names what it read.** That matters after cleanup, because the built-in rows are gone by
then and the Scripts check has to fall back to what the migrated Projects publish. A report that says
`the built-in rows` was checked against them directly, and one that says `the migration journal` was
checked against what the migration recorded. Without that, a green report after cleanup would be
indistinguishable from a check that had nothing left to look at.

Two `warning` results are ordinary rather than faults, and both are restated on every run because
each is operator work that stays outstanding until somebody does it: a permission that carried
constraints and was left withdrawn for you to recreate, and a Job that names a built-in script module
rather than a Script, which no Custom Script Project can hold.

## What is not part of this release

| Area | Status |
|---|---|
| A complete write fence | Not possible for a plugin. See [Crossing the fence](#crossing-the-fence). |
| Choosing a different grouping | Not planned. Edit the staged Projects afterwards if you want a different shape. |
| Reversing a cutover | Not planned. Restore from a database backup. |
