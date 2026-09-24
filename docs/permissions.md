# Permissions

Running a Script, changing its source and choosing its active revision require
different permissions. For example, permission to rename a Project does not
allow someone to change the code it serves.

Grant actions through a NetBox Object Permission for the relevant model. Use
object-level constraints to limit access to particular Projects or Scripts.

## Script Project

| Action | What it allows |
|---|---|
| `view` | View Projects, their Script Files and current state, including the active revision. |
| `add` | Create a Project. Creating one through **Upload Script** also requires Script File `add`. |
| `change` | Edit Project fields, subject to the activation requirements below, and upload more scripts. Uploads also require Script File `add`. |
| `delete` | Delete a Project and its associated revisions and Scripts. |
| `activate` | Activate or deactivate a revision, repair published Scripts, and change `activation_policy`, `data_source` or `data_path`. |
| `migrate` | Migrate the installation from NetBox's built-in Custom Scripts. |
| `reconcile` | Rebuild a Project's source from its Data Source directory on demand. |

**Activation and source settings.** The Project's **Activate** and **Repair
Scripts** actions, and the revision-level **Activate** and **Deactivate**
actions, all require `activate`.

Changing `activation_policy`, `data_source` or `data_path` on an existing
Project requires both `change` and `activate`. This applies to the edit form,
bulk edit, bulk import updates and REST. Creating a Project or submitting an
unchanged value does not require this additional activation check.

Activation permission must cover the Project being edited, not just another
Project. The check uses its stored values. A constraint on a source setting
can therefore allow one change that moves the Project outside that constraint.

If a source setting changes between authorization and saving, the edit is
refused without writing anything. This can also affect a description-only edit.
The form shows an error, and REST returns `400`. Reload the Project and submit
again.

**Revision history.** The Project's `view` permission lets you see its active
revision on the Project page. Browsing the **Revisions** and **Revision Files**
tabs, or opening a revision's page, also requires
`netbox_scripts.view_scriptprojectrevision`. The tabs are hidden without it.

The UI's **Deactivate** action is on the **Revisions** tab. To reach it, grant
Project `view` and `activate` along with Revision `view`. Activation permission
alone does not make that button accessible.

## The Migration page

Migration uses Script Project permissions. The page, inventory, staging and
verification require `add`. Cutover and the three passes that follow it require
`migrate`.

| Pass | Action | What it changes |
|---|---|---|
| The page itself, and one migration's detail | `add` | Nothing. |
| **Run inventory** | `add` | Reports on the built-in feature without changing it. |
| **Stage Projects** | `add` | Creates or reuses Projects and stages revisions without activating them. |
| **Verify** | `add` | Reports on migration results without changing the objects checked. |
| **Enter cutover** | `migrate` | Withdraws captured permissions, disables captured Event Rules, cancels waiting runs and removes built-in synchronization registrations. |
| **Activate Projects** | `migrate` | Activates eligible staged revisions. |
| **Repoint references** | `migrate` | Moves supported Event Rules, permissions, Job history and schedules. Shared grants permanently lose their built-in Report coverage. |
| **Clean up** | `migrate` | Deletes eligible mapped built-in modules and their stored source. |

Grant `migrate` only to the people responsible for the migration. These passes
make changes that have no automatic rollback. A user with only `add` can use
the page, inventory, staging and verification, but not the other four passes.

See [Migration](migration.md) for the maintenance requirements, retained modules
and Report-access implications. These permissions do not provide a complete
barrier against new built-in Script runs.

## Script

| Action | What it allows |
|---|---|
| `view` | View Scripts, their execution defaults and run history. |
| `change` | Edit enabled state, execution overrides, comments, owner, tags and custom fields. |
| `run` | Run a Script through the UI or REST. |
| `schedule` | Request a future or recurring run. |

Scripts are published by activation, not created or deleted directly. When a
revision stops publishing a Script, its row is retired rather than deleted.

Scheduling requires the `schedule` permission and the author's
`scheduling_enabled` setting. Without either, the run form hides **Schedule
at** and **Recurs every**. Over REST, a caller without `schedule` receives
`403` when submitting `schedule_at` or `interval`. See
[Scheduling a run](execution.md#scheduling-a-run) for the execution settings.

## An Event Rule authorizes a run without the run permission

An Event Rule can run a Script without checking `run_netboxscript` for either
the rule's author or the user whose action triggered it. The run is attributed
to the triggering user, not the rule's author.

**Treat `extras.add_eventrule` and `extras.change_eventrule` as permissions to
arrange Script execution.** Creating a rule or changing its action can start
Scripts that the rule's author cannot run directly. Both the form and REST
allow the action type and target to be changed.

Restrict these permissions to people trusted to arrange those runs. See
[Event Rules](event-rules.md#permissions) for how the rule authorizes execution.

## Two privileges with no codename of their own

**Selecting Script Files** requires Script File `change` to access the
selection route. Creating a declaration also requires `add` on that row.
Changing an existing declaration requires `change` on the row both before and
after the edit. Unchanged rows need no additional child-write permission.

Uploads require Script File `add` to access the route. Each new declaration
must meet the user's `add` constraints. Re-enabling an existing declaration
also requires `change` on that row. Project permission does not replace these
checks. A refusal rolls back the declaration operation before source ingestion
is scheduled.

Changing the selection queues a refresh of the Project's accepted source.
Treat Script File permissions as source-management permissions.

Script File `delete` does not enable a deletion route, even though NetBox lists
it as a standard model action. Deselecting a file disables its declaration
rather than deleting it.

**Viewing execution results** requires `core.view_job`. Logs and output are
stored on NetBox Jobs and use that permission, not a separate plugin action.
