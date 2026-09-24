# Script File

A Script File selects one Python module in a Project for discovery. Validation
imports that module to find the Scripts it publishes. Helper modules remain
importable without a declaration, so the Script File list is not an inventory
of every source file.

Staging records the enabled declarations in each revision's
[Script file snapshot](scriptprojectrevision.md#script-file-snapshot). Changing
or disabling a declaration affects future snapshots, not revisions already
staged.

## Fields

FK means foreign key. Fields marked `system` are maintained by validation, not
edited through forms or APIs.

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project. Cannot change after creation |
| `source_path` | string | yes | Canonical POSIX-style path relative to the Project root. Cannot change after creation |
| `enabled` | boolean | yes | Whether staging includes the declaration in new revision snapshots. Defaults to `true` |
| `discovery_status` | choice | system | `pending`, `discovered`, `no_scripts` or `failed`. Result of the most recent validation of the current declaration |
| `discovery_error` | text | system | Why discovery failed or the file published no Scripts. Empty otherwise |
| `last_discovered_revision` | FK | system | Revision whose validation last updated the discovery fields |

The discovery fields describe the current declaration, not an editable verdict.
A full model save reloads their stored values to preserve validation updates.

A Script File has no separate name. Its Project and canonical source path form
its identity and cannot be changed. Use the selection workflow below when a
source file moves or is renamed.

## Renaming or moving a script file

After a source file moves, its new path appears as a candidate on the Project's
**Script Files** tab and the old path is reported as missing:

1. Select the new path. A new declaration starts at `pending` until validation.
2. Deselect the old path. This sets its `enabled` field to `false`.

Deselecting keeps the old declaration and its discovery information. The
`unique_project_source_path` constraint also reserves disabled paths. If a path
returns, selecting it re-enables the original row.

Only enabled declarations enter new revision snapshots. Disabling a declaration
does not change existing snapshots. There is no standalone delete route for a
Script File.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `ScriptProject` | yes | `on_delete=CASCADE`, reverse name `script_files` |
| `last_discovered_revision` | `ScriptProjectRevision` | no | `on_delete=SET_NULL`, no reverse accessor |

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/script-files/` |
| GraphQL | `netbox_script_file` / `netbox_script_file_list` |

Use the Project's **Script Files** tab to select from importable modules at any
directory depth. Candidates are grouped by directory, with top-level files
under `(root)`. Each option shows its filename, so you do not need to type a
path. The Project selection is also available over REST:

```text
GET  /api/plugins/netbox-scripts/projects/<id>/script-files/
PUT  /api/plugins/netbox-scripts/projects/<id>/script-files/   {"paths": [...]}
```

`GET` reports candidates with `selected`, `available` and `discovery_status`.
It requires Project `view` permission. `PUT` replaces the selection and rejects
paths that do not exist in the Project's source.

`PUT` requires Project `change` and Script File `change` permission to enter the
route. Creating a declaration also requires `add` permission on the new row.
Changes to existing declarations require `change` permission on each affected
row before and after the change. Unchanged rows need no additional child write
permission. See
[Permissions](../permissions.md#two-privileges-with-no-codename-of-their-own)
for the complete selection and upload rules.

Script Files also have list, detail, edit, filtering and global-search views for
troubleshooting across Projects, plus REST and GraphQL access. They have no
top-level navigation item, create, delete, bulk delete, bulk import or UI bulk
edit route. REST POST and DELETE return HTTP 405. Bulk PUT and PATCH remain
available on the Script File list endpoint.

Discovery fields are readable and filterable, but cannot be changed through
these interfaces. `last_discovered_revision` is a nested revision in REST and a
revision relationship in GraphQL. Revisions also have their own read-only REST
and GraphQL endpoints.

## Validation rules

Declaration paths are checked before revision validation:

- Paths are canonicalized using the storage rules and must satisfy the
  [source path policy](../configuration.md#source-path-policy).
- The file must end in `.py`. Directory names and the module name without `.py`
  must be Python identifiers, not reserved keywords. The Project root
  `__init__.py` cannot be declared because it names the package itself.
- Paths must not collide when letter case is ignored, at any directory level.
  This rejects both `Utils.py` with `utils.py` and `Lib/deploy.py` with
  `lib/audit.py`, avoiding conflicts on case-insensitive filesystems.
- Paths must resolve to distinct module names. For example, `pkg.py` and
  `pkg/__init__.py` both name `pkg`, and the package would shadow the module.

## Invariants

| Invariant | Enforcement |
|---|---|
| One path is declared at most once per Project | `unique_project_source_path` database constraint |
| The Project cannot be changed | `clean()` reports the field error, and `save()` also checks ORM writes |
| The source path cannot be changed | `clean()` and `save()` compare canonical paths, so equivalent spellings are not changes |
| The stored path is canonical | `save()` normalizes the path before it can enter a snapshot |
| The stored path is importable | `save()` rejects paths that cannot identify an importable module |
| Discovery fields are system-managed | `editable=False`, only validation writes them, and a full `save()` reloads their stored values |
| `last_discovered_revision` belongs to the same Project | `clean()` check |

Script Files are installation-global. Under NetBox Branching they read and
write the main schema, like Projects and revisions. A revision's declarations
do not vary by branch.

Code that bypasses model validation, such as `QuerySet.update()` or raw SQL,
must supply canonical paths itself.
