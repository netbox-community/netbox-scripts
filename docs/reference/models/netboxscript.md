# Script

A Script represents one Python Script class published from an activated Project
revision. Runs and their Job history belong to this object, so its identity
continues across revisions of the source.

You do not create Script rows manually. Validation records the discovered
classes, and [activation](../runtime.md) creates or updates their rows. Script
publication and the Project's active-revision update commit in one transaction.

## Identity

A Script's identity combines its Project, the dotted path of the module that
**defines** the class, and the class name.

Scripts belong to a Project rather than a [Script File](scriptfile.md). A Script
File can use `script_order` to publish a class from a helper module that has no
Script File declaration. Re-exporting that class from several files still
produces one Script identity. The [revision](scriptprojectrevision.md) records
which Script File published it as provenance, not as its parent relationship.

Moving a class to another module creates a new identity. The row for its old
location is retired.

## Fields

FK means foreign key. Fields marked `system` are managed by the plugin. See
[API](#api) for the fields you can edit.

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project |
| `module_path` | string | yes | Dotted path of the Project module that defines the class |
| `class_name` | string | yes | Name of the Script class |
| `display_name` | string | system | `Meta.name`, or the class name when unset |
| `description` | text | system | `Meta.description`, or empty when unset |
| `enabled` | boolean | yes | Whether this Script may run. Defaults to `true` |
| `commit_default_override` | boolean | no | Overrides the class commit default. Empty follows the class |
| `job_timeout_override` | integer | no | Overrides the class timeout in seconds. Minimum 1. Empty follows the class |
| `notifications_default_override` | choice | no | Overrides the class notification policy. Empty follows the class |
| `is_retired` | boolean | system | Whether the active revision has stopped publishing this Script |
| `last_seen_revision` | FK | system | Revision whose activation most recently published this Script |
| `metadata` | JSON | system | Class execution defaults copied from the revision during activation |

Use `comments` for operational notes. Unlike `description`, it is yours to edit
and is not replaced by metadata from the class.

Synchronization maintains the system-managed fields. Forms and API inputs do
not accept changes to them. A full model save reloads their stored values so an
edit started before activation does not restore older metadata.

## Enabled versus retired

**`enabled` is an administrator setting.** Use it to allow or prevent execution.
Synchronization never changes it, including when the Project is revalidated or
the Script is retired and published again.

**`is_retired` is a publication status.** The plugin sets it when the active
revision stops publishing the class. Retirement keeps the row, its primary key
and its Job history. Publishing the same class again reuses the row and retains
its `enabled` setting.

A Script is available for a new run when it is enabled, is not retired, its
Project is enabled and the Project has an active revision. Deactivation retires
the Project's Scripts in the same transaction that clears its active revision.
For the behavior of already queued runs, see [Running Scripts](../../user-guide/execution.md).

Change `enabled` through the Script edit form, bulk edit on the list page or a
REST PATCH.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `ScriptProject` | yes | `on_delete=CASCADE`, reverse name `scripts` |
| `last_seen_revision` | `ScriptProjectRevision` | no | `on_delete=SET_NULL`, no reverse accessor |

Deleting a Project removes its Scripts, Script Files and revisions. If a
referenced revision is removed, `last_seen_revision` is cleared without deleting
the Script.

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/scripts/` |
| GraphQL | `netbox_script`, `netbox_script_list` |
| UI | Scripts > Scripts |

REST supports list, detail and update, but not creation or deletion. A PATCH can
set `enabled`, the three execution overrides, `comments`, `owner`, tags and
custom fields. See
[Override execution defaults](../../user-guide/execution.md#override-execution-defaults)
for how the overrides resolve.

Invalid values on writable fields return HTTP 400. Read-only fields and unknown
keys, including misspellings, are ignored rather than rejected. Check the
response to confirm the intended fields changed.

The UI offers list, detail, edit and bulk edit, with no add, delete or bulk
import. A Project's detail page lists all Scripts it has published, including
retired ones.

## Running

Request a run from the Script's page. Running requires the `run` permission,
separately from `change`, and each occurrence is recorded as a Job on the Script.

A one-shot run pins the revision active when it is requested. A recurring run
resolves the active revision when each occurrence starts. See
[Running Scripts](../../user-guide/execution.md) for scheduling, permissions and results.

## Invariants

| Invariant | Enforcement |
|---|---|
| One class is published at most once per Project | `unique_project_module_class` database constraint |
| Derived fields are system-managed | `editable=False`, only synchronization writes them |
| A retired Script keeps its primary key | Synchronization never deletes a row |
| An administrator's `enabled` survives synchronization | Synchronization excludes this field from its writes |
| An unchanged Script emits no change-log entry | Synchronization compares values and skips rows that already match |

Synchronization also runs when the same revision is activated again. Skipping
matching rows avoids redundant change-log entries and, on paths that deliver
events, unnecessary update events.

Scripts are installation-global. Under NetBox Branching they read and write the
main schema, regardless of the active branch. See
[NetBox Branching](../../administration/branching.md#installation-wide-objects)
for the shared model policy.
