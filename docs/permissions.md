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
| `view` | See Projects, their Revisions, their Modules and their state |
| `add` | Create a Project, including the Upload form that creates one from a file |
| `change` | Edit a Project's own fields, and upload a further script into one |
| `delete` | Delete a Project, which cascades its Revisions and Custom Scripts |
| `activate` | Put a Revision into service, and stand a Project down from one |
| `reconcile` | Rebuild a Project's source from its Data Source directory on demand |

`activate` covers three surfaces: the **Activate** button on a Project, and the
per-row **Activate** and **Deactivate** buttons on its Revisions tab. All three
change what the Project serves, so all three ask for the same action. A Revision
has no permission of its own, because it has no surface anyone would grant one
for.

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
