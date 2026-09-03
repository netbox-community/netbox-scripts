# Permissions

Managing what code a Project runs is more privileged than running it. That
principle is why this plugin does not settle for the four standard actions:
someone trusted to run a Custom Script is not thereby trusted to change the
source it runs from, and someone who may rename a Project is not thereby
allowed to choose the code it serves.

Grant these like any other NetBox permission, by ticking the action on an
Object Permission for the relevant model. Object-level constraints work
throughout, so a permission can be narrowed to particular Projects or Scripts.

## Custom Script Project

| Action | What it allows |
|---|---|
| `view` | See Projects, their Modules and their state, including the revision in force |
| `add` | Create a Project, including the Upload form that creates one from a file |
| `change` | Edit a Project's own fields, and upload a further script into one |
| `delete` | Delete a Project, which cascades its Revisions and Custom Scripts |
| `activate` | Put a Revision into service, stand a Project down from one, and repair its Custom Scripts |
| `migrate` | Move this installation off the built-in Custom Scripts feature |
| `reconcile` | Rebuild a Project's source from its Data Source directory on demand |

`activate` covers four surfaces: the **Activate** and **Repair Scripts**
buttons on a Project, and the per-row **Activate** and **Deactivate** buttons on
its Revisions tab. All four decide what the Project serves or what it publishes,
so all four ask for the same action. **Repair Scripts** republishes the rows of
the revision already in force, which is the same write activation makes, so it
is the same privilege.

**Browsing revisions is a separate permission, `netbox_custom_scripts.view_customscriptprojectrevision`.**
The line falls between what a Project is serving and its history. A Project's own
page reports the revision in force, so `view` on the Project is enough to see
that. The Revisions tab, the Files tab and a revision's own page list the
history and its stored file paths, so each asks for the Revision view
permission, and the two tabs are hidden without it.

That matters for one workflow in particular. **Deactivate** is reached only from
a row on the Revisions tab, so standing a Project down needs `view` and
`activate` on the Project **and** Revision view. Granting `activate` alone
leaves the button out of reach.

## The Migration page

The Migration page has no model of its own, so its passes take Custom Script
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
| **Repoint references** | `migrate` | Rewrites Event Rules, Object Permissions, Job history and schedules |
| **Clean up** | `migrate` | Deletes the built-in script modules this migration mapped, and their stored source |

`migrate` authorizes irreversible changes to rows this plugin does not own, so
grant it to the person running the migration and not as a matter of course. A
user holding `add` alone still sees the page, the inventory and staging, and is
not offered the four steps past the fence.

## Custom Script

| Action | What it allows |
|---|---|
| `view` | See Custom Scripts, their execution defaults and their run history |
| `change` | Edit the administrator's fields: enabled, comments, owner, tags, custom fields |
| `run` | Run a Custom Script, from the UI or over REST |
| `schedule` | Ask for a run at a set time or on a recurrence |

Custom Scripts have no `add` or `delete` action in practice. Rows are derived
from an activated Revision rather than authored, and a Script the active
Revision stops publishing is retired rather than deleted.

`schedule` composes with the author's own `scheduling_enabled` setting. The
setting says the script may be run unattended, the permission says this user may
ask for it, and both have to hold. A user with `run` but not `schedule` gets the
run form without **Schedule at** and **Recurs every**, exactly as a script whose
author disabled scheduling does, so there is one path to those fields being
absent rather than two. Over REST there is no form to leave them out of, so
`schedule_at` and `interval` are refused with a 403 instead.

## Two privileges with no codename of their own

Both of these are deliberate. A second name for the same privilege would mean
granting it twice and choosing between the spellings.

**Changing which modules are entrypoints** requires the Custom Script Module
`change` action. A Module *is* an entrypoint declaration, so that action already
describes the privilege exactly. Changing the selection restages the Project's
source, which makes the Module permission a source-management one in its own
right.

**Viewing execution results** requires the NetBox `core.view_job` permission.
Run logs and output live on the Job, so NetBox's own Job permission governs
them. A plugin-local duplicate would be enforced here and ignored by NetBox's
own Job views.
