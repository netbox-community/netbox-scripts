# Permissions

Managing what code a Project runs is more privileged than running it. That
principle is why this plugin does not settle for the four standard actions:
someone trusted to run a Script is not thereby trusted to change the
source it runs from, and someone who may rename a Project is not thereby
allowed to choose the code it serves.

Grant these like any other NetBox permission, by ticking the action on an
Object Permission for the relevant model. Object-level constraints work
throughout, so a permission can be narrowed to particular Projects or Scripts.

## Script Project

| Action | What it allows |
|---|---|
| `view` | See Projects, their Script Files and their state, including the revision in force |
| `add` | Create a Project, including the Upload form that creates one from a file. The Upload form also needs Script File `add` |
| `change` | Edit a Project's own fields, except the three that decide what it serves, and upload a further script into one. Adding a script also needs Script File `add` |
| `delete` | Delete a Project, which cascades its Revisions and Scripts |
| `activate` | Put a Revision into service, stand a Project down from one, repair its Scripts, and move `activation_policy`, `data_source` or `data_path` |
| `migrate` | Move this installation off the built-in Custom Scripts feature |
| `reconcile` | Rebuild a Project's source from its Data Source directory on demand |

`activate` covers four surfaces: the **Activate** and **Repair Scripts**
buttons on a Project, and the per-row **Activate** and **Deactivate** buttons on
its Revisions tab. All four decide what the Project serves or what it publishes,
so all four ask for the same action.

It also covers three fields, on every surface that can write them. `change` alone
renames a Project and edits its description. Moving `activation_policy`,
`data_source` or `data_path` decides what the Project serves next, so each needs
`activate` as well, on the edit form, in bulk edit, in a bulk import record that
updates an existing Project, and over REST. A record that creates a Project is
not affected, since there is nothing to move away from. Submitting a field's
stored value is not a move either. **Repair Scripts** republishes the rows of
the revision already in force, which is the same write activation makes, so it
is the same privilege.

**Browsing revisions is a separate permission, `netbox_scripts.view_scriptprojectrevision`.**
The line falls between what a Project is serving and its history. A Project's own
page reports the revision in force, so `view` on the Project is enough to see
that. The Revisions tab, the Revision Files tab and a revision's own page list the
history and its stored file paths, so each asks for the Revision view
permission, and the two tabs are hidden without it.

That matters for one workflow in particular. **Deactivate** is reached only from
a row on the Revisions tab, so standing a Project down needs `view` and
`activate` on the Project **and** Revision view. Granting `activate` alone
leaves the button out of reach.

## The Migration page

The Migration page has no model of its own, so its passes take Script
Project actions. Which one depends on what the pass changes, and the split is
the same principle as `activate`: the passes below the fence only report or
create Projects, while everything from the cutover onwards rewrites and deletes
rows of the built-in feature.

| Pass | Action | What it changes |
|---|---|---|
| The page itself, and one migration's detail | `add` | Nothing |
| **Run inventory** | `add` | Nothing, it is a report |
| **Stage Projects** | `add` | Creates Projects and their Revisions, every one inactive |
| **Verify** | `add` | Nothing, it is a report |
| **Enter cutover** | `migrate` | Withdraws every grant on the built-in feature, disables its Event Rules, cancels its queued jobs, deregisters its synchronization |
| **Activate Projects** | `migrate` | Puts the staged Projects into service |
| **Repoint references** | `migrate` | Rewrites Event Rules, Object Permissions, Job history and schedules. A grant that also covered built-in Reports loses that coverage here, permanently |
| **Clean up** | `migrate` | Deletes the built-in script modules this migration mapped, and their stored source |

`migrate` authorizes irreversible changes to rows this plugin does not own, so
grant it to the person running the migration and not as a matter of course. A
user holding `add` alone still sees the page, the inventory and staging, and is
not offered the four steps past the fence.

## Script

| Action | What it allows |
|---|---|
| `view` | See Scripts, their execution defaults and their run history |
| `change` | Edit the administrator's fields: enabled, execution overrides, comments, owner, tags, custom fields |
| `run` | Run a Script, from the UI or over REST |
| `schedule` | Ask for a run at a set time or on a recurrence |

Scripts have no `add` or `delete` action in practice. Rows are derived
from an activated Revision rather than authored, and a Script the active
Revision stops publishing is retired rather than deleted.

`schedule` composes with the author's own `scheduling_enabled` setting. The
setting says the script may be run unattended, the permission says this user may
ask for it, and both have to hold. A user with `run` but not `schedule` gets the
run form without **Schedule at** and **Recurs every**, exactly as a script whose
author disabled scheduling does, so there is one path to those fields being
absent rather than two. Over REST there is no form to leave them out of, so
`schedule_at` and `interval` are refused with a 403 instead.

## An Event Rule authorizes a run without the run permission

An Event Rule that names a Script runs it whenever the rule fires, and the
run is attributed to the user whose action triggered the event, not to whoever
wrote the rule. Nothing checks that the rule's author holds `run_netboxscript`,
and nothing checks it for the triggering user either.

**So `extras.add_eventrule` and `extras.change_eventrule` are both privileged
grants here.** Anyone who can create an Event Rule, or repoint an existing one,
can arrange for a Script to run without holding the permission that
governs running one directly. An existing rule's action type and action object
are editable on the form and over REST, so the two permissions carry the same
escalation. Grant either to the same people you would grant `run_netboxscript`.

This is not a gap this plugin introduced. NetBox's own built-in script action
behaves the same way. **No hook a plugin can implement has the rule's author in
scope**, so the author cannot be checked at all: `validate()` receives only the
action object and its data, and runs on every save including ones with no user
behind them. Refusing the triggering user instead would be possible, and is
deliberately not done, because they did not write the rule and denying them a run
they never asked for is the wrong answer. It is recorded here because an
administrator sizing up `run_netboxscript` should know the second route exists.

## Two privileges with no codename of their own

Both of these are deliberate. A second name for the same privilege would mean
granting it twice and choosing between the spellings.

**Changing which files are script files** requires the Script File `change`
action to enter the selection route. Newly created declarations additionally
require `add` permission on those rows. Changes to existing declarations require
`change` permission on each affected row before and after the change. Unchanged
rows require no additional child write permission.

The upload routes retain their Script File `add` entry requirement. Each created
declaration is checked against its `add` constraints. Re-enabling an existing
declaration requires its `change` permission as well. Project permission does not
substitute for these child permissions. A refusal rolls back the declaration
operation before source ingestion is scheduled.

Changing the selection queues a refresh of the Project's accepted source, so
Script File permissions are source-management permissions in their own right.

Script File `delete` grants nothing. The picker offers it because it is a standard
model action, but no route removes a declaration: deselecting it on the Project
disables the row instead.

**Viewing execution results** requires the NetBox `core.view_job` permission.
Run logs and output live on the Job, so NetBox's own Job permission governs
them. A plugin-local duplicate would be enforced here and ignored by NetBox's
own Job views.
