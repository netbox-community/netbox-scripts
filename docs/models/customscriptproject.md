# Custom Script Project

A Custom Script Project represents one complete and internally consistent
source tree. It is the ownership boundary for source files and the Python
package boundary used when loading and executing scripts.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Human-readable, mutable display name |
| `key` | slug | yes | Stable user-facing project key, unique |
| `storage_key` | UUID | auto | Immutable storage and runtime identity, assigned on creation |
| `source_type` | choice | yes | `upload` (default) or `data_source` |
| `data_source` | FK | conditional | Required for `data_source` projects, not allowed for uploads |
| `data_path` | string | no | Directory path to the project root within the data source |
| `activation_policy` | choice | yes | `manual` (default) or `automatic_if_valid` |
| `active_revision` | FK | auto | Currently active revision, set only by the storage activation service |
| `enabled` | boolean | yes | Whether the project is active, defaults to `true` |
| `description` | string | no | Free-form description |
| `comments` | text | no | Free-form operational notes |

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `data_source` | `core.DataSource` | conditional | `on_delete=PROTECT`, no reverse relation, set only when `source_type` is `data_source` |
| `revisions` | `CustomScriptProjectRevision` | no | Reverse of the revision's `project`, `on_delete=CASCADE`. Every snapshot ever staged for this project |
| `active_revision` | `CustomScriptProjectRevision` | no | `on_delete=PROTECT`, reverse name `active_revision_for`. Set only by the storage activation service |

Each project owns a history of immutable source snapshots, documented on the
[Custom Script Project Revision](customscriptprojectrevision.md) page. Modules
and discovered scripts are planned follow-up models.

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/custom-scripts/custom-script-projects/` |
| GraphQL | `custom_script_project` / `custom_script_project_list` |

## Invariants

| Invariant | Enforcement |
|---|---|
| `key` cannot change after creation | `clean()`, the edit form disables the field, REST returns 400 |
| `source_type` cannot change after creation, pending a dedicated source-transition workflow | `clean()`, the edit form disables the field, REST returns 400 |
| `storage_key` never changes | `save()` guard, the field is excluded from forms and read-only in REST |
| `data_path` is stored canonically: POSIX-style, relative, single separators, no leading `./` or trailing `/` | `clean()` and the REST serializer normalize. Absolute paths, `..` traversal, and backslashes are rejected |
| `data_source` projects require a non-empty `data_path`. The repository root is not a valid project root | `clean()` plus the `enforce_source_ownership` database constraint |
| `upload` projects carry no `data_source` and no `data_path` | `clean()` plus the `enforce_source_ownership` database constraint |
| `active_revision` must belong to this project | `clean()` |
| Deleting a project clears `active_revision` first, so its own revision cannot protect it | `delete()` override, inside one transaction |

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must supply
canonical values themselves. The database constraint enforces the ownership
rules but not path spelling.

`QuerySet.delete()` never calls the model's `delete()`, so it does not clear
`active_revision` and a project that is serving one of its own revisions raises
`ProtectedError`. Clear the pointer first, or delete through the model. Storage
cleanup itself is unaffected, because the cleanup receivers rule out Django's
signal-free fast-delete path.

No user-facing surface hits this. Both NetBox's bulk-delete view and its REST
bulk destroy iterate the selected objects and call each one's `delete()`, so the
caveat applies only to migrations, housekeeping commands, tests, and internal
plugin code.

## Identity notes

`storage_key` is the project's internal storage and runtime identity: it will
name physical storage, runtime packages, and cache paths. It is exposed
read-only for troubleshooting, but it is not the public identity of a project
or its scripts. Integrations must reference projects by `key` (or object ID),
never by `storage_key`.

## NetBox Branching (provisional)

Custom Script Projects are intended to be installation-global runtime
identities: project definitions and source revisions are not expected to be
copied or to diverge per branch. The storage layer now settles half of this.
A revision's on-disk location is a pure function of the project's
`storage_key` and the revision's `digest`, with no branch or schema context,
so no branch-aware path handling is expected. Whether script execution is
branch-aware remains an execution-model decision that lands with the
execution work. This posture is subject to ratification in the concept
review, and a netbox-branching compatibility test is planned.

## Limitations

| Limitation | Impact |
|---|---|
| Modules and discovered scripts are not yet modeled | Source snapshots exist as revisions, but nothing yet reads a package out of one |
| No user-facing way to stage a revision | Uploads and Data Source synchronization arrive in a follow-up release |
