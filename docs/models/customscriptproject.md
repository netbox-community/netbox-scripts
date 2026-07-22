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
| `enabled` | boolean | yes | Whether the project is active, defaults to `true` |
| `description` | string | no | Free-form description |
| `comments` | text | no | Free-form operational notes |

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `data_source` | `core.DataSource` | conditional | `on_delete=PROTECT`, no reverse relation, set only when `source_type` is `data_source` |

Project revisions, modules, and discovered scripts are planned follow-up
models. The project's `active_revision` reference lands together with the
revision model.

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

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must supply
canonical values themselves. The database constraint enforces the ownership
rules but not path spelling.

## Identity notes

`storage_key` is the project's internal storage and runtime identity: it will
name physical storage, runtime packages, and cache paths. It is exposed
read-only for troubleshooting, but it is not the public identity of a project
or its scripts. Integrations must reference projects by `key` (or object ID),
never by `storage_key`.

## NetBox Branching (provisional)

Custom Script Projects are intended to be installation-global runtime
identities: project definitions and future source revisions are not expected
to be copied or to diverge per branch. Whether script execution is
branch-aware is an execution-model decision that lands with the revision and
execution work. This posture is subject to ratification in the concept
review. Compatibility testing with netbox-branching is planned alongside the
revision and storage models.

## Limitations

| Limitation | Impact |
|---|---|
| Revisions, modules, and scripts are not yet modeled | The project is a container only. Source handling arrives with the revision model |
