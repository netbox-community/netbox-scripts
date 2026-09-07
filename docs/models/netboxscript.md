# Script

A Script is one Script class a validated revision publishes. It is the
object a run is requested against and the object a Job history belongs to, so it
outlives any single revision of the project's source.

Rows are derived rather than authored. Nothing creates one by hand: project
validation records the classes a revision publishes, and
[activation](../runtime.md) turns that record into rows in the same transaction
that moves the project's active pointer. A reader therefore sees either the old
revision with its old scripts or the new revision with its new ones.

## Identity

A Script is identified by its project and by the dotted module path and
class name of the module that **defines** it.

The parent is the Project rather than the [Script
File](scriptfile.md), which may look surprising, because a script file is
what discovery imports. The reason is `script_order`: a class defined in a
helper file can be published by a script file that re-exports it, and helper
files have no Script File row, because Script File rows are declarations only.
Deriving identity from the defining module keeps one class one script no matter
how many script files re-export it, and there would be no Script File row to point at
in the helper case. Which script file published a class is recorded as provenance
inside the [revision's](scriptprojectrevision.md) snapshot, not as a
relational parent.

A class that moves to a different file becomes a new identity, and the row for
its old location is retired.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project |
| `module_path` | string | yes | Dotted path of the project module that defines the Script class |
| `class_name` | string | yes | Name of the Script class |
| `display_name` | string | system | `Meta.name`, defaulting to the class name |
| `description` | text | system | `Meta.description`, empty when the class declares none |
| `enabled` | boolean | yes | Whether this Script may be executed. Default is true |
| `is_retired` | boolean | system | Set when the active revision no longer publishes this Script |
| `last_seen_revision` | FK | system | The revision whose activation last published this Script |
| `metadata` | JSON | system | Execution defaults the most recent validation read from the class |

Everything marked system is owned by synchronization. No form, serializer, or
GraphQL input accepts those fields.

`description` is a text field rather than the 200-character field the base model
provides, because `Meta.description` has no length of its own and truncating an
author's text would be lossy. Since it is system-managed, there is nothing for an
administrator to type into and nothing for synchronization to overwrite.
Operational notes belong in `comments`, which is yours.

## Enabled versus retired

Two separate booleans, and the distinction is the point.

`enabled` is the administrator's. It answers "should this be allowed to run",
and **synchronization never touches it**. A script you disable stays disabled
when its project is re-validated, re-activated, retired, and published again.

`is_retired` is synchronization's. It answers "does the active revision still
publish this", and you never set it. A script the active revision stops
publishing is retired rather than deleted, which preserves the row's primary key
and with it the Job history it has accumulated. Publish the class again and the
same row returns, with its history and with whatever `enabled` you had left it
at.

A script is executable when it is enabled, not retired, its project is enabled,
and its project is serving a revision. That last condition is normally implied by
the others, because standing a project down retires every script it publishes in
the same transaction, but it is checked in its own right so a run always resolves
its class out of source that is actually being served.

Set `enabled` from the Script edit form, in bulk from the list view, or
with a REST PATCH. Because synchronization never writes it, an activation cannot
undo an administrator's decision.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `ScriptProject` | yes | `on_delete=CASCADE`, reverse name `scripts` |
| `last_seen_revision` | `ScriptProjectRevision` | no | `on_delete=SET_NULL`, no reverse accessor |

Deleting a Script File leaves its scripts alone, since the publishing
script file is provenance rather than a parent. Deleting the project takes its
scripts with it. Pruning an old revision never takes scripts with it, so
`last_seen_revision` simply becomes empty.

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/scripts/` |
| GraphQL | `netbox_script`, `netbox_script_list` |
| UI | Scripts > Scripts |

Update only. Rows are derived from a validated revision, so the API offers list,
detail, and update, and refuses to create or delete. A PATCH may set `enabled`,
`comments`, `owner`, tags, and custom fields. Every derived field is read only,
and a value supplied for one is ignored rather than rejected.

The intent is to reject a write a client could reasonably believe took effect
and to ignore one to a field the client never authored, but the split the API
actually makes is by **value, not by key**. A bad value on a writable field is a
400. Any other key is dropped in silence, whether it is a derived field like
`module_path` or a plain misspelling, because DRF reads the request by iterating
the writable fields and never looks at a key that is not one of them.

The UI has the same shape: list, detail, edit, and bulk edit, with no add, no
delete, and no bulk import. Retirement replaces deletion, so a script that stops
being published keeps its primary key and the Job history attached to it.

A project's detail page carries a Scripts panel listing everything that
project has published, retired scripts included.

## Running

A Script is run from its own page, against the revision its project is
serving when the run is requested. Running is its own permission, `run`, granted
separately from `change`, and every run is recorded as a Job attached to the row.
See [Running Scripts](../execution.md).

## Invariants

| Invariant | Enforcement |
|---|---|
| One class is published at most once per project | `unique_project_module_class` database constraint |
| Derived fields are system-managed | `editable=False`, only synchronization writes them |
| A retired script keeps its primary key | Synchronization never deletes a row |
| An administrator's `enabled` survives synchronization | The field is absent from everything synchronization writes |
| An unchanged script emits no change-log entry | Synchronization compares before saving and skips a row that already matches |

That last one is load bearing rather than an optimization. Activation
synchronizes every time it runs, including when it re-activates the revision
already in force, so an unconditional save would log a change and queue an event
for every script on every activation.

Scripts are installation-global, like projects, revisions, and script files.
Under NetBox Branching they read and write the main schema from every branch,
because the scripts an installation offers cannot differ per branch.
