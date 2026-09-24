# Migration

Use this workflow to move NetBox's built-in Custom Scripts into NetBox Scripts.
It checks your installation, stages source as Script Projects and moves
supported references. Review each pass's results for items that need manual
attention.

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
| Inventory | Checks built-in Custom Script source, proposes Projects and counts affected references. | No changes to the built-in feature to undo. |
| Staging | Creates or reuses Projects, declares Script Files and stages revisions for validation. Activates nothing. | Remove only objects created by the migration. Review changes to reused Projects separately. |
| Cutover | Captures references and queued input, then closes the permissions, rules, runs and synchronization it controls. | No automatic rollback. |
| Activation | Activates eligible revisions and publishes their Script rows. | The built-in feature remains closed. |
| Repointing | Moves supported Event Rules, permissions and Job history, then recreates eligible captured runs. | Review unresolved and skipped items in the results. |
| Cleanup | Deletes mapped built-in modules and stored source where no protected references remain. Records completion. | No automatic undo. |
| Verification | Checks migration results without changing the objects being checked. | Safe to repeat at any stage. |

Each pass is a background Job with its own log and results. **Keep an RQ worker
running, including during cutover.** Stopping all workers also stops migration
and revision validation.

Follow the order above and review each result. A completed Job does not
necessarily satisfy the next pass's prerequisites. Verification can run before,
between or after the other passes.

### Worker arrangement

Built-in Script runs, plugin Script runs and migration passes use the `default`
queue unless configured otherwise. One worker can serve several queues in
sequence. Several workers can execute Jobs from the same queue at once, so
separate queue names do not prevent concurrent execution.

**Use one worker throughout cutover.** It must serve the migration queue and
all queues needed for the handover. Check existing Jobs' queue names as well as
`QUEUE_MAPPINGS`. Changing a mapping does not move queued work. Migration passes
have no associated object and normally use `default`.

Before capture, cutover checks all registered workers, not just those serving
Script queues. It refuses if there is more than one unless **Accept concurrent
workers** is selected. This checks worker count only at that moment. Keep
additional workers stopped throughout the pass, including any your deployment
might restart automatically.

!!! warning "A cancelled run can still execute"

    One worker prevents a second worker from running built-in Custom Scripts
    alongside cutover. It does not stop RQ's scheduler from queueing a task it
    fetched just before cancellation, so that task can still execute after
    cutover. The reference pass then holds back its replacement, because the
    run has started or its task is queued again.

    **Accept concurrent workers** bypasses the check and records the worker
    names. It also allows another worker to take a run during cancellation.
    Cancellation never overwrites a run a worker has started, and the reference
    pass holds back the replacement of one that starts later. A run whose Job
    row is deleted before the reference pass leaves nothing to check, so it is
    recreated.

    One worker does not prevent users, integrations or Event Rules from
    submitting new work. The maintenance window is still required.

A **migration run** stores the state, captured references and input in a journal,
and each pass's results. Only one run can be open at a time. Its state moves
forward through `legacy`, `staging`, `cutover` and `migrated`.

## Before you start

Inventory can be run ahead of the maintenance window. Complete the following
preparation before selecting **Enter cutover**.

1. **Arrange a maintenance window and stop new submissions.** Coordinate with
   users, integrations and automation owners. Prevent new built-in Script runs
   and source changes throughout the handover. The plugin's permission changes
   do not restrict superusers or every other way of granting access.
2. **Check workers and existing runs.** Keep the single-worker arrangement above
   in place for cutover. Let running built-in Scripts finish. Cutover refuses
   while any are running and lists them. Review waiting and scheduled work too.
   It can become due before cutover starts, even during the maintenance window.
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
next action. If a Project stops serving a revision, activation may be suggested
again. A source or validation problem can leave the page without a suggested
action.

**Run inventory** is safe to repeat. The page lists blockers and summarizes
warnings. Open its Job for the complete report.

**Stage Projects** asks for confirmation and refuses a second queued staging
pass. The worker runs a fresh inventory check. **Any blocking finding stops the
entire pass before it creates Projects, declarations or revisions.** Once the
check passes, review each Project's staging result.

**Enter cutover** requires confirmation that you took the backup. **Activate
Projects** and **Repoint references** become available after their prerequisites
are met and do not ask for a second confirmation. **Clean up** confirms before
removing built-in modules and their stored source.

Each action returns to the Migration page with a link to its Job. Read the log
and results even when the Job finishes successfully. It may report skipped
Projects, unresolved references or an incomplete cutover.

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

An enabled permission may therefore no longer cover Reports. Recreate the
required Report access and subscriptions explicitly. Re-enabling an old row
does not restore object types removed by repointing.

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

These counts show which references inventory found, not how many it can
migrate. Inventory does not rewrite them.

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

The import check excludes imports under supported `TYPE_CHECKING` or
always-false guards, imports inside functions and imports handled by
`except ImportError`. It reads source without proving that every remaining
import or runtime path will succeed.

An unreadable or unparsable module becomes a finding. Inventory still reports
on the remaining modules.

## What grouping produces

A Project holds one source tree that loads as a Python package. Migration
groups built-in modules by their recorded paths.

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

Repeating staging reuses the same Projects. Unchanged source and Script File
selection can reuse a revision. Changes can create another revision or add
declarations to an existing Project, so a repeated pass can still make changes.

Validation runs separately. Read each revision's verdict and errors rather than
treating the staging Job's completion as approval. See
[Putting a revision in service](data-sources.md#putting-a-revision-in-service).

A module that publishes no Script is staged as a helper without a Script File
declaration. Staging recognizes Scripts from executable built-in rows or
classes in the source. This also lets it select code already using the plugin
API.

Inventory warns about modules that appear to publish nothing. Check these
warnings: the module may be an intentional helper, or its Script may no longer
load. Correct the source and repeat inventory and staging before cutover.

## Crossing the fence

**Cutover has no automatic rollback.** It captures references, records the
`cutover` state, then closes the parts of the built-in feature it controls.

The journal keeps the original permissions, Event Rules, migration map and
readable input from waiting runs. Retries preserve those records. While cutover
is incomplete, capture adds unrecorded waiting runs, including new recurring
occurrences. Completed cutover steps are not repeated.

If the pass fails after entering `cutover`, resolve the reported problem and
select **Enter cutover** again. The state alone does not confirm completion.
A recorded capture can also prevent further staging even while the state still
reads `staging`. Migration passes that change data run one at a time across
workers.

Cutover affects four areas:

| Area | Effect |
|---|---|
| Permissions | Disables captured Object Permissions. Shared Report coverage is affected too. |
| Event Rules | Disables captured rules. Rules whose action runs a Report are excluded. |
| Queued runs | Deletes reachable waiting tasks and marks their Jobs failed, unless a worker has already started one. Records cancellation outcomes and warns when input or execution outcomes prevent automatic replay. |
| Synchronization | Removes synchronization registrations for the built-in Custom Script modules. |

Captured permissions and Event Rules are closed before any waiting run is
cancelled, and cancelling a run sends no Job-completion event. These changes do
not block every way to submit or change built-in Scripts. Superusers,
`DEFAULT_PERMISSIONS`, plain Django grants and already-authorized work can
remain unaffected. Do not resume submissions yet.

Cutover refuses if staging has not run, the migration state does not allow it,
built-in Custom Script Jobs are running or the worker check fails. Every mapped
Project must exist and either serve a revision or have a `valid` revision
available. A pending or invalid newest revision does not block a Project that
already has a usable one.

This check reads revision state, not source storage. Missing files can still
make activation fail. Review activation results before repointing.

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

Check each outcome and active revision before repointing. Repeating activation
checks the Projects again and can make further changes.

## Repointing what the installation refers to

**Repoint references** replays the journal in four steps.

| Step | What it moves |
|---|---|
| Event Rules | Replaces supported built-in action targets and source object types. A fully moved rule is re-enabled only if it was enabled when captured. |
| Permissions | Maps supported actions and object types. Mixed grants are split to preserve unrelated coverage. Constrained grants and unmappable actions are reported. |
| Job history | Associates supported built-in Script Jobs with their replacement Scripts. Module-level history has no equivalent target. |
| Schedules | Recreates eligible captured runs against replacement Scripts. |

The activation pass must be recorded before repointing starts. Event Rules,
history and schedules also require every remaining mapped Project to serve a
revision. Permissions map object types rather than individual Scripts, so that
additional requirement does not apply to them.

Recreate constrained permissions manually. Neither copying nor dropping their
old constraints preserves the intended access. Check mixed grants too:
withdrawal temporarily affects the whole captured permission, including any
unrelated object types.

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
recurrences can execute before cleanup and verification finish.** The worker
running the reference pass finishes that Job first, but replayed runs do not
wait for the maintenance window to end.

Runs recorded as already executing or executed are not automatically recreated.
Review their outcome and arrange any required future schedule manually. A run
recorded as cancelled that later started, or was queued again, on the built-in
side is held back rather than recreated, as the warning under
[Worker arrangement](#worker-arrangement) explains.

A reference step completes when no retryable work remains. A missing
replacement Script can leave it open. Once complete, the step returns its
recorded result rather than scanning for new references.

**Permanently skipped** means migration will not retry the item, not that you
cannot resolve it manually. Examples include classes absent from the captured
map, module-level references, constrained permissions, overdue one-shot runs,
invalid input and deleted or deactivated recorded owners. Review the warnings
separately. These items do not all keep migration open.

Retained built-in rows may lose their pages when NetBox removes the feature.
Export or reconcile history you need before that upgrade. Retaining rows does
not guarantee they will remain accessible.

## Retiring the built-in rows

**Clean up** removes built-in modules and their source files. It runs only after
all four reference steps have completed. Deleting a built-in Script can also
delete its attached Job history, so check the reference results first.

Cleanup considers mapped modules individually. It leaves unmapped modules
alone and does not delete a mapped module while its replacement Project is
present but serves no revision.

The results distinguish two kinds of retained work.

**Retained** modules do not keep migration open. They hold module-level Job
history or history for classes no longer published by their source. There is
no replacement identity for that history. Keep it unless you have made a
separate retention decision.

**Blocked** modules keep migration open. They have unresolved or retired
replacement Scripts, history still to move, remaining Event Rule references
or replacement Projects without an active revision. Follow the reported reason:
correct the source, activate the required revision or resolve the reference,
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
reference updates, removed synchronization registrations and module deletions
persist. Some modules remain for their history. Removing the plugin restores
none of the original database or storage state.

Someone who can manage Object Permissions can grant `extras.add_scriptmodule`
again, allowing new built-in Custom Scripts to be uploaded. Migration does
not permanently disable that feature.

**One worker prevents a second worker from executing alongside cutover, not
all duplicate execution.** The scheduler can still queue a cancelled task,
and runs can start between an interrupted pass and its retry. Check existing
work before retrying. See [Worker arrangement](#worker-arrangement).

Replay uses recorded cancellation outcomes and each Job's current state. A run
recorded as cancelled that started, or was queued again, on the built-in side
is held back rather than recreated. That is not an exactly-once guarantee: a
run whose Job row is deleted before the reference pass leaves nothing to check.
Keep the maintenance window and review results even when cutover reports
completion.

## Recovery

### Before cutover

You can stop without closing the built-in feature. Remove only Projects and
revisions created by your trial migration. Staging results record `created` and
`revision_created` for each successful Project result.

Staging can change an existing Project's declarations or accepted source.
Compare reused Projects with their pre-migration state rather than deleting
everything in the report. Removing staged content does not reset the journal.

### After cutover

There is no automatic rollback. A run in `cutover` may still have an unfinished
cutover step. Resolve the reported problem and use **Enter cutover** again to
resume it. Do not restart staging merely because a pass failed.

The journal supports retries of unfinished work, but cannot recreate every
captured run. Review missing queue tasks, unsupported input, invalid forms,
ownership changes and permanent skips manually.

Investigate a Job still marked running after its worker disappears. Check the
workers, queues and any external changes the Script may have made. An absent
worker does not prove that the Script never ran. Do not delete or replay the
Job on that basis alone.

To reverse the installation, restore a consistent pre-cutover database and both
source stores with the matching software versions. **This does not restore the
queue or undo changes to devices and other external systems.**

Before restoring, stop execution against the installation. Review built-in and
replacement plugin tasks before restarting workers. Restoring the database
does not remove newer Redis tasks. Restoring Redis without checking its contents
can replay unrelated or already-executed work.

A pre-cutover database backup does not include the journal captured later.
After a restore, use your separate record of pending runs to recreate required
work. Check what actually executed before submitting replacements.

If the run's state, journal and Job result disagree after an interruption,
preserve them for investigation. Do not open another migration or manually
advance the state just to make the page appear complete.

## Checking whether it landed

**Verify** normally reports five checks, each with a level, message and evidence
source. The overall status is the highest level reported. If no migration run
exists, the report says there is nothing to verify.

| Check | What it examines |
|---|---|
| Modules | Recorded Projects, their active revisions and unmapped built-in modules. |
| Scripts | Replacement identities and retirement state. After built-in modules are gone, it checks publication in the migrated Projects instead. |
| Event Rules | Remaining built-in references and captured rules still disabled. |
| Permissions | Remaining built-in grants, including those left for manual recreation. |
| Jobs | Remaining built-in history, recorded recreations, failed replacement Jobs and task presence for recorded replacements still waiting. |

Warnings can mean a step has not run or manual work remains, such as recreating
constrained permissions or reviewing retained history. Read each message rather
than treating warnings as harmless.

A replacement Job recorded by migration produces a **blocking** result if it
is still waiting but has no RQ task. Repeating the reference pass does not repair
its completed recreation record. Investigate the missing task and recreate the
required run manually.

An unreachable queue produces a warning, not a report of lost tasks. Task
checks also wait until the reference pass finishes. Run verification again
once the queue is reachable and repointing is complete.

Job records may be removed after execution by retention cleanup. A missing ID
alone does not prove a recurrence was lost. Verification does not trace every
recurring occurrence, test every Script or confirm its external actions.
Use Job history and your operational checks alongside the report.

## What is not part of this release

| Area | Status |
|---|---|
| A complete write fence | Not provided. Keep a maintenance window and control submissions. See [Crossing the fence](#crossing-the-fence). |
| Choosing a different grouping | No migration grouping selector is provided. Arrange the source layout before staging and cutover. |
| Reversing a cutover automatically | Not provided. See [Recovery](#recovery). |
