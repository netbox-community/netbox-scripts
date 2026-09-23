# Migration

Use the migration workflow to move NetBox's built-in Custom Scripts into NetBox
Scripts. It inventories your installation, stages the source as Script Projects,
and moves supported references onto the plugin.

!!! warning "Alpha migration"

    NetBox Scripts is in alpha and is not recommended for production use.
    Rehearse the migration in a test environment using copies of your database
    and source storage. Keep test workers isolated from production queues.

    Inventory leaves the built-in feature unchanged. Staging creates or updates
    plugin objects without activating them. **Cutover has no automatic rollback.**
    Read [Crossing the fence](#crossing-the-fence) and [Recovery](#recovery) before
    starting it.

## What the seven passes do

| Pass | What it does | Recovery |
|---|---|---|
| Inventory | Reads built-in Custom Script modules, checks their source, proposes Projects and counts affected references. | No changes to the built-in feature to undo. |
| Staging | Creates or reuses Projects, declares Script Files and stages revisions for validation. Activates nothing. | Remove only objects created by the migration. Review changes to reused Projects separately. |
| Cutover | Captures references and queued input, withdraws captured permissions, disables captured Event Rules, cancels waiting runs and removes built-in synchronization registrations. | No automatic rollback. |
| Activation | Activates eligible revisions and publishes their Script rows. | The built-in feature remains closed. |
| Repointing | Moves supported Event Rules, permissions and Job history, then recreates eligible captured runs. | Review unresolved and skipped items in the results. |
| Cleanup | Deletes mapped built-in modules and stored source where no protected references remain. Records completion. | No automatic undo. |
| Verification | Checks migration results without changing the objects being checked. | Safe to repeat at any stage. |

Every pass runs as a background Job and records its log and results there.
**Keep an RQ worker running, including during cutover.** Stopping all workers
also stops migration and revision validation.

Follow the order above and check each result before continuing. Passes have
specific prerequisites, not simply a requirement that the preceding Job
completed. Verification can run before, between or after the other passes.

### Worker arrangement

By default, built-in Script runs, plugin Script runs and migration passes use
`default`. Queue names alone do not prevent concurrent execution: one worker can
consume several queues sequentially, while several workers can execute Jobs from
one queue at the same time.

**Use one worker throughout cutover.** It must consume the migration queue and
all queues needed for the handover. Check the queue names recorded on existing
Jobs as well as `QUEUE_MAPPINGS`. Changing a mapping does not move work already
queued. Migration passes are not associated with an object and normally use
`default`.

Cutover checks registered workers before capture and refuses when it sees more
than one, unless **Accept concurrent workers** is selected. This is a preflight
check, not a lock on worker startup. Keep additional workers stopped throughout
the pass, including workers your deployment might restart automatically. The
check counts registered workers, not only those subscribed to a particular
Script queue.

!!! warning "A cancelled run can still execute"

    One worker prevents a second worker from executing built-in Custom Scripts
    beside the cutover. It does not stop RQ's scheduler, which every NetBox
    worker runs, from queueing a run it fetched just before the cutover
    cancelled it. That run can then execute after the pass and also be
    recreated by the plugin. This applies even when **Accept concurrent
    workers** is clear.

    **Accept concurrent workers** bypasses the worker-count refusal and records
    the worker names on the migration run. A second worker can then also take a
    built-in run while cutover is cancelling it. The Job row and migration
    journal cannot reliably distinguish every such outcome, so do not rely on
    worker count or cancellation status alone to prevent duplicate execution.
    One worker does not prevent users, integrations or Event Rules from
    submitting new work either, so the maintenance window is still required.

A **migration run** records the state, captured journal and pass results. Only
one run can be open at a time. Its state moves forward through `legacy`,
`staging`, `cutover` and `migrated`.

## Before you start

Inventory can be run ahead of the maintenance window. Complete the following
preparation before selecting **Enter cutover**.

1. **Arrange a maintenance window and stop new submissions.** Coordinate with
   users, integrations and automation owners. Prevent new built-in Script runs
   and source changes throughout the handover. The plugin's permission changes
   do not restrict superusers or every other way of granting access.
2. **Check workers and existing runs.** Keep the single-worker arrangement above
   in place for cutover. Let running built-in Scripts finish. Cutover names and
   refuses running Jobs, but waiting and scheduled work can still become due
   before it starts. Review that work rather than assuming the maintenance
   window pauses it.
3. **Take a consistent backup.** Back up the database, built-in source storage
   and plugin source storage together as one restore point. A mismatched
   restore can leave revisions pointing to missing source files. Record the
   NetBox and plugin versions, along with the inputs, owners, commit settings,
   queue names, times and intervals of pending runs you may need to recreate.
   These backups do not restore RQ task data. See [Recovery](#recovery).
4. **Finish Data Source synchronization.** Synchronize each source under
   *Operations > Integrations > Data Sources* and wait for completion. Avoid
   further synchronization while staging and reviewing its revisions. On a
   first migration, staging creates the Projects. Afterwards, check each
   Project's revision list. Use **Reconcile Source** to restage a directory that
   changed, then wait for validation again.
5. **Resolve inventory blockers and review warnings.** Check existing Project
   conflicts, Reports, constrained permissions and scheduled inputs. Confirm
   that staged Projects have usable revisions before cutover. A successful
   staging Job does not mean every revision passed validation.

## Starting a pass

Open *Scripts > Migration*. The page shows the latest Job for each pass and the
current state of proposed or staged Projects. **Not staged** means inventory
proposed a Project that staging has not created.

Buttons are numbered in workflow order. The highlighted button suggests the
next available action. A Project that stops serving can make activation the next
action again. When a source or validation problem needs attention, the page may
have no suggested button.

**Run inventory** is safe to repeat. The page lists blockers and summarizes
warnings. Open its Job for the complete report.

**Stage Projects** asks for confirmation and refuses a second queued staging
pass. The worker runs a fresh inventory check. If it finds any blocking
finding, the entire pass stops before creating Projects, declarations or
revisions. After that check passes, review each Project's staging result.

**Enter cutover** requires confirmation that you took the backup. **Activate
Projects** and **Repoint references** become available after their prerequisites
are met and do not ask for a second confirmation. **Clean up** confirms before
removing built-in modules and their stored source.

Each action returns to the Migration page with a link to its Job. Read both the
log and recorded results. A Job can finish while reporting skipped Projects,
unresolved references or an incomplete cutover.

Inventory, staging and verification require permission to **add** a Script
Project. Cutover, activation, repointing and cleanup require the separate Script
Project **`migrate`** action. The page itself requires `add`, and reading Job
results requires *Core > Jobs* view permission. See
[Permissions](permissions.md#the-migration-page).

## Reports are not covered

Report modules with the `reports` file root are excluded from inventory and
staging. Their Jobs, synchronization registrations and Event Rules whose action
runs a Report are not migrated. That exclusion also applies when a Report's
Event Rule watches built-in Script object types.

**Shared permissions and event subscriptions still affect Reports.** Both
Reports and Custom Scripts use `extras.script` and `extras.scriptmodule`.
Permissions and source subscriptions on those types cannot be separated by file
root. Cutover withdraws the captured grants and rules, and repointing replaces
their built-in coverage with plugin coverage where possible.

A restored permission can therefore be enabled without granting access to
Reports. Recreate the required Report access and subscriptions explicitly.
Re-enabling an old row does not restore object types that repointing removed.

Inventory reports the number of excluded Report modules as a warning. The
plugin does not support their authoring API. Rewrite a Report as a Script before
moving its functionality into the plugin.

## Reading the inventory

The inventory report is stored in the Job's data.

| Key | What it contains |
|---|---|
| `status` | The highest finding level: `ready`, `warning` or `blocking`. |
| `modules` | Built-in Custom Script modules, their paths, file roots, dialects and recorded classes. |
| `reports` | The number of excluded Report modules. |
| `projects` | Proposed Project keys, names, source types and member modules. |
| `dialects` | Module counts by authoring dialect. |
| `references` | Counts of affected Event Rules, permissions and Jobs. |
| `findings` | Levels, codes, affected modules or paths, and messages explaining what needs attention. |

Reference counts describe the scope of the built-in feature being examined.
They are not a promise that every reference has a plugin counterpart. Inventory
does not rewrite them.

## What the three statuses mean

| Status | Meaning |
|---|---|
| `ready` | Inventory found no warnings or blockers. This is not a validation verdict. |
| `warning` | Staging can proceed, but review the findings and any manual work they identify. |
| `blocking` | The entire staging pass is refused. Resolve every blocking finding before trying again. |

`legacy_import` identifies source using `extras.scripts`. NetBox deprecated its
built-in Custom Scripts in 4.7.0 and schedules their removal for v5.0 in
[netbox#22935](https://github.com/netbox-community/netbox/issues/22935) and
[netbox#22938](https://github.com/netbox-community/netbox/issues/22938). Use the
warning list to plan the move to the plugin's authoring API before upgrading.
See [Authoring](authoring.md) for supported imports and compatibility limits.

`import_unresolvable_in_branch` identifies an unavailable import inside a
conditional branch. Inventory cannot determine whether an arbitrary branch runs
in your deployment. Review it, then use the revision's validation result to
check the staged source.

The eight blocking codes are:

| Code | What to check |
|---|---|
| `report_style` | A class has Report-style `test_` methods without `run()`. Rewrite it as a Script. |
| `not_importable` | A source filename cannot be used as a Python module identifier by the plugin. Rename it, for example by replacing a hyphen with an underscore. |
| `unparsable` | The stored source is not valid Python. Correct the syntax. |
| `source_unreadable` | Inventory could not read the stored source. Restore access or the missing file. |
| `import_unresolvable` | Inventory cannot resolve an unconditional external import on this host. Check the dependency or use a relative import for a Project-local helper. |
| `data_source_root` | Grouping would create a Project at the Data Source root. Move the scripts into a directory within the source. |
| `project_not_manual` | A Project that staging would reuse has a non-manual activation policy. Set it to Manual before staging. |
| `project_conflict` | An existing Project overlaps the proposed directory within the same Data Source. Resolve the overlap. |

The import check excludes supported `TYPE_CHECKING` and always-false guards,
function-body imports and imports handled by `except ImportError`. It is a
source-analysis check, not proof that every remaining import or runtime path
will succeed.

An unreadable or unparsable module becomes a finding. It does not prevent
inventory from reporting on the remaining modules.

## What grouping produces

A Project is a source tree and a Python package boundary. Migration groups
built-in modules using their recorded paths.

| Source | Grouping |
|---|---|
| Data Source | One Project per script-holding directory, identified by its Data Source and path. |
| Upload | One Project per module, containing that file. |

A nested script-holding directory joins the Project above it. The parent tree
already includes its subdirectories, so migration does not create overlapping
Projects.

For a Data Source Project, staging copies the whole directory, not only the
files the built-in feature registered as scripts. Helpers and resources in that
tree are available to the migrated scripts. Use relative imports for
Project-local Python helpers.

## Staging

Staging creates or reuses each proposed Project and stages its source. New
Projects use **Manual** activation. Reused Projects must also have that policy.
Staging itself does not activate a revision.

Repeating staging reuses the same Project identities. Unchanged content and
Script File selection can reuse an existing revision. Changed source or
selection can create another revision, so a repeat is not necessarily a no-op.
Staging can also add declarations to a reused Project.

Validation runs separately. Read each revision's verdict and errors rather than
treating the staging Job's completion as approval. See
[Putting a revision in service](data-sources.md#putting-a-revision-in-service).

A module that publishes no Script is staged as a helper without adding a
Script File declaration. Publication is determined from executable built-in
rows or a class recognized in its source, so code already using the plugin API
can still be selected for discovery.

Inventory warns about modules that appear to publish nothing. Check these
warnings: the module may be an intentional helper, or its Script may no longer
load. Correct the source and repeat inventory and staging before cutover.

## Crossing the fence

**Cutover has no automatic rollback.** It captures references first, records the
`cutover` state, and then closes the parts of the built-in feature it controls.

The journal preserves the original permissions, Event Rules, migration map and
readable waiting-run inputs. Retries keep those records instead of replacing
them with the already-modified state. While cutover is incomplete, capture also
adds previously unrecorded waiting runs, including new recurring successors.
Completed cutover steps are not replayed.

A failure after the state transition leaves the run in `cutover` with the step
unfinished. Return to **Enter cutover** after resolving the reported problem.
The state alone does not prove that the step completed. A recorded capture can
also prevent staging from running again even if the state still reads
`staging`. Mutating migration passes serialize their work across workers.

Cutover affects four areas:

| Area | Effect |
|---|---|
| Permissions | Disables captured Object Permissions. Shared Report coverage is affected too. |
| Event Rules | Disables captured rules. Rules whose action runs a Report are excluded. |
| Queued runs | Deletes reachable waiting tasks and marks their Jobs failed. Records cancellation outcomes and warns when input or execution outcomes prevent automatic replay. |
| Synchronization | Removes synchronization registrations for the built-in Custom Script modules. |

These changes do not form an installation-wide write fence. Superusers,
`DEFAULT_PERMISSIONS`, plain Django grants and already-authorized work can
remain outside the permission closure. Do not resume submissions yet.

!!! warning "Completion events during cutover"

    During cutover, waiting Jobs are cancelled before captured Event Rules
    are disabled. Cancellation can therefore trigger enabled Job-completion
    rules, even with one worker. Review those rules and
    any resulting work as part of the maintenance procedure. Do not assume
    cutover suppresses all Event Rule actions.

Cutover refuses before staging, outside an allowed migration state, while
built-in Custom Script Jobs are running, or when the worker check fails. It also
checks that every mapped Project exists and either serves a revision or has a
`valid` revision available. A pending or invalid newest revision does not by
itself block a Project that already has a usable revision.

This readiness check reads revision state, not source storage. Bytes removed
after validation can still cause activation to fail. Review activation results
before proceeding to repointing.

A missing queue task leaves no input to capture, so the run requires manual
recreation. Uploaded files and other unsupported input values are omitted from
the journal with a warning. Replay validates the remaining input: it can be
refused, or an optional omitted value can use its default. **Do not assume that
a run with dropped input is an equivalent replacement.**

## Activating the staged Projects

**Activate Projects** runs after cutover and publishes the Script rows needed
for repointing. It records an outcome for each Project. One activation failure
does not stop the remaining Projects.

The pass selects the newest `valid` revision unless the Project already serves
its newest revision. In that case, it synchronizes the published rows to
repair any differences. It does not select `retired` revisions.

!!! warning "Review Projects that already serve a revision"

    This selection can choose an older `valid` revision even when a newer
    revision is active. Do not assume the pass preserves the current active
    revision. Review Projects with multiple revisions before running it.

Check the recorded outcomes and each Project's active revision before
repointing. Repeating activation re-evaluates Projects. It is not an
unconditional no-op.

## Repointing what the installation refers to

**Repoint references** replays the journal in four steps.

| Step | What it moves |
|---|---|
| Event Rules | Replaces supported built-in action targets and source object types. A fully moved rule is re-enabled only if it was enabled when captured. |
| Permissions | Maps supported actions and object types. Mixed grants are split to preserve unrelated coverage. Constrained grants and unmappable actions are reported. |
| Job history | Associates supported built-in Script Jobs with their replacement Scripts. Module-level history has no equivalent target. |
| Schedules | Recreates eligible captured runs against replacement Scripts. |

Activation must have recorded its pass before any of these steps runs. Event
Rules, history and schedules also require every remaining mapped Project to
serve a revision. Permissions do not have that additional serving requirement,
because they map object types rather than individual Scripts.

Constrained permissions remain withdrawn for manual recreation. Copying their
old constraints or silently dropping them would not preserve the intended
access. Review permissions that combine built-in Script types with unrelated
types, because the withdrawal temporarily affects the whole captured grant.

### Schedule timing and ownership

Replay applies both checks below.

**Timing**

| Captured run | Replay behavior |
|---|---|
| Future schedule | Keeps its scheduled time. |
| Overdue recurrence | Becomes eligible to run immediately and keeps its interval. Its timing shifts. |
| Overdue one-shot schedule | Is skipped and reported for manual rescheduling. |
| Pending run without a scheduled time | Becomes eligible to run immediately. |

**Ownership**

| Owner | Replay behavior |
|---|---|
| Owner still active and permitted to run the Script | Runs under that account. |
| Owner lacks run permission | Remains outstanding. Grant permission and retry the reference pass. |
| Recorded owner was deleted after capture, or is deactivated | Is skipped. Recreate it under an appropriate account. |
| No owner recorded at capture | Remains ownerless. Migration does not assign a new owner. |

Replay preserves the captured commit setting. **Pending runs and overdue
recurrences can execute before cleanup and verification finish.** A worker busy
with the reference pass must finish that Job first, but the maintenance window
does not delay replayed runs until the end of migration.

Runs recorded as already executing or executed are not automatically recreated.
Review their outcome and arrange any required future schedule manually. A run
recorded as cancelled that still executed is recreated as well, as the warning
under [Worker arrangement](#worker-arrangement) explains.

Each reference step records completion when no retryable work remains. A missing
replacement Script can leave it open for another attempt. Completed steps
return their recorded results rather than scanning for newly added references.

**Permanently skipped** means migration will not retry the item. It does not
mean there is no manual remedy. Examples include classes absent from the frozen
map, module-level references, constrained permissions, overdue one-shot runs,
invalid input and deleted or deactivated recorded owners. Read the warnings and
resolve these separately. They do not all keep the migration open.

Preserved built-in rows may lose their normal interface when NetBox removes the
built-in feature. Export or otherwise reconcile history you need before that
upgrade. The removal plan is not a guarantee that retained rows will remain
accessible.

## Retiring the built-in rows

**Clean up** removes built-in modules and their source files. It runs only after
all four reference steps have completed. Deleting a built-in Script can also
delete its attached Job history, so check the reference results first.

Cleanup considers mapped modules individually. It leaves unmapped modules
alone and does not delete a mapped module while its replacement Project is
present but serves no revision.

The results distinguish two kinds of retained work.

**Retained** modules do not keep migration open. These hold module-level Job
history or history for classes no longer published by their source. The
migration has no replacement identity for that history. Keep it unless you have
made a separate retention decision.

**Blocked** modules keep migration open. Causes include unresolved or retired
replacement Scripts, history that should have been moved, remaining Event Rule
references, and replacement Projects that serve no revision. Follow the stated
reason. Correct source, activate the required revision or resolve the reference,
then retry.

A reference added after its step completed may need manual handling. Repeating
a completed reference step does not necessarily move it. Do not delete history
solely to clear a blocker.

The run reaches `migrated` when cleanup has no blocking work left. Retained
modules are listed in the cleanup results and migration warnings. Verification
is useful afterwards, but does not replace that detailed retention record.

## After the last pass

1. **Restart every web and worker process.** Processes can retain imported
   built-in modules after their rows and files are removed.
2. **Run Verify and review the results.** Check warnings, manually recreated
   permissions, retained history and schedule outcomes before ending the
   maintenance window.
3. **Resume normal operation.** Re-enable submissions and restore your normal
   worker arrangement when the handover has been checked.

Replayed automation may already have run by this point. Verification does not
pause it or reverse its effects.

### Two guarantees, and what each one is worth

**Disabling the plugin does not reverse migration.** Permission changes,
reference updates, synchronization deregistration and deleted modules persist
in NetBox's database and storage. Some modules may have been retained for
history. Removing the plugin does not restore the original installation.

Someone who can manage Object Permissions can grant `extras.add_scriptmodule`
again, allowing new built-in Custom Scripts to be uploaded. Migration does
not permanently disable that feature.

**The single-worker arrangement prevents a second worker starting a built-in
run while cutover executes.** It does not stop submissions or prevent a run
starting between an interrupted pass and its retry. Check existing work before
retrying. It also does not stop RQ's scheduler queueing a run again that fell
due as cutover cancelled it, so that run can execute after the pass. The pass
uses recorded cancellation intent and Job state to determine which captured
runs should be replayed.

With one worker or with **Accept concurrent workers**, a run can execute and
also be recorded as cancelled. Its replacement can then execute too. Neither
the journal nor the Job row provides an exactly-once guarantee in either mode.

These limits are why the maintenance window and result review remain necessary,
even when cutover reports completion.

## Recovery

### Before cutover

You can stop without closing the built-in feature. Remove only Projects and
revisions created by your trial migration. Staging results record `created` and
`revision_created` for each successful Project result.

Staging can reuse a pre-existing Project and change its declarations or accepted
source. Compare it with the pre-migration state rather than deleting everything
the report names. Removing staged content is not the same as resetting the
migration run's journal.

### After cutover

There is no automatic rollback. A run in `cutover` may still have an unfinished
cutover step. Resolve the reported problem and use **Enter cutover** again to
resume it. Do not restart staging merely because a pass failed.

Forward recovery uses the journal to retry supported unfinished work. It does
not guarantee that every captured run can be recreated. Missing queue tasks,
unsupported input, invalid forms, ownership changes and permanent skips require
manual review.

A Job still marked running after a worker disappears needs its own investigation.
Check worker and queue state and any external changes the Script may have made.
Do not delete or replay it merely because no worker is currently registered.
The migration cannot establish from that fact alone that the Script never ran.

To reverse the installation, restore a consistent pre-cutover database and both
source stores with the matching software versions. **This does not restore the
queue or undo changes to devices and other external systems.**

Before restoring, prevent execution against the changing installation. Review
both old built-in tasks and replacement plugin tasks before restarting workers.
A database restore does not remove newer Redis tasks, and blindly restoring
Redis can replay unrelated or already-executed work.

A pre-cutover database backup lacks the journal captured later. Use the separate
record of pending runs made before cutover to recreate required work after a
reversal. Reconcile what actually executed before submitting replacements.

If the run's state, journal and Job result disagree after an interruption,
preserve them for investigation. Do not open another migration or manually
advance the state just to make the page appear complete.

## Checking whether it landed

**Verify** normally reports five checks. Each has a level, a message and the
source of its evidence. The overall status is the highest level reported. If no
migration run exists, it reports that there is nothing to verify.

| Check | What it examines |
|---|---|
| Modules | Recorded Projects, their active revisions and unmapped built-in modules. |
| Scripts | Replacement identities and retirement state. After built-in modules are gone, it checks publication in the migrated Projects instead. |
| Event Rules | Remaining built-in references and captured rules still disabled. |
| Permissions | Remaining built-in grants, including those left for manual recreation. |
| Jobs | Remaining built-in history, recorded recreations, failed replacement Jobs and task presence for recorded replacements still waiting. |

A step not yet performed generally produces a warning. Warnings can also mean
manual work remains, such as constrained permissions or retained history. Read
the messages rather than treating every warning as harmless.

A recorded recreated Job that is still waiting but has no RQ task produces a
**blocking** result. Repeating the reference pass does not repair its completed
recreation record. Investigate the missing task and recreate the required run
manually.

An unreachable queue produces a warning, not a claim that tasks were lost.
Verification also avoids inspecting tasks while the reference pass is still
working. Run it again once the queue is reachable and repointing has finished.

Recorded Job IDs may disappear after execution and retention cleanup. Their
absence is not by itself proof of a missing recurrence. Verification does not
trace every recurring successor, test every Script or establish whether its
external actions succeeded. Use the Job history and your operational checks
alongside the report.

## What is not part of this release

| Area | Status |
|---|---|
| A complete write fence | Not provided. Keep a maintenance window and control submissions. See [Crossing the fence](#crossing-the-fence). |
| Choosing a different grouping | No migration grouping selector is provided. Arrange the source layout before staging and cutover. |
| Reversing a cutover automatically | Not provided. See [Recovery](#recovery). |
