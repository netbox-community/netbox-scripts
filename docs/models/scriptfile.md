# Script File

A Script File declares one executable file within a Script
Project: a Python file of the project's source tree that discovery
imports and publishes Scripts from. Helper files need no Script File row,
they stay importable by the script files without being one, so a script file list is
the project's explicit statement of what runs rather than an inventory of every
file.

Enabled script file declarations are frozen into each revision at staging time as
that revision's [script file snapshot](scriptprojectrevision.md). Editing,
disabling, or deleting a script file therefore changes future revisions and never
what an existing revision was validated against.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Script Project |
| `source_path` | string | yes | POSIX-style relative path of the script file, stored in canonical form |
| `enabled` | boolean | yes | Whether staging includes this declaration in new revision snapshots, default true |
| `discovery_status` | choice | system | `pending`, `discovered`, `no_scripts`, or `failed`, describing the most recent validation of the current declaration |
| `discovery_error` | text | system | Why discovery failed, or why a script file published nothing, empty otherwise |
| `last_discovered_revision` | FK | system | The revision whose validation last wrote these discovery fields |

The three discovery fields are system-managed. Project validation writes them,
no form or serializer accepts them, and they describe the current declaration.

A script file has no name of its own. It is identified by its project and its source
path, and both are frozen once the script file exists, so a declaration can never be
repointed at a different file. That keeps the discovery fields honest: a
repointed row would report a verdict for a file it no longer names.

## Renaming or moving a script file

There is no rename. When a file moves or is renamed in the project's source, the
new path appears as a candidate on the Script Files tab and the old one is
reported as missing. Move the new path into the selected list and the old one
out of it:

1. Selecting the new path declares it, starting at `pending` until the next validation.
2. Deselecting the old path clears its `enabled`, which is what selection means.

Deselecting disables rather than deletes, which keeps the old declaration's
discovery history and keeps its path reserved, because
`unique_project_source_path` does not consider `enabled`. Renaming the file back
therefore re-enables the original row instead of colliding with it. Staging includes only enabled declarations, so
a disabled one stops reaching new revisions immediately while the revisions it
was already snapshotted into keep meaning what they meant.

Deselecting is the only way to retire a declaration. There is no delete route on any
surface, because a declaration is a Project setting rather than an object managed on
its own.

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

Script Files are selected on the owning Project's **Script Files** tab, which
lists the importable modules of its source at any depth and never asks for a
typed path. Candidates are grouped by the directory they sit in, with files at
the top of the source under `(root)`, and each option carries its own file name
rather than the whole path. The same operation is available over REST:

```text
GET  /api/plugins/netbox-scripts/projects/<id>/script-files/
PUT  /api/plugins/netbox-scripts/projects/<id>/script-files/   {"paths": [...]}
```

`GET` reports every candidate with `selected`, `available`, and its
`discovery_status`. `PUT` replaces the selection, refusing any path the project
has no source file at. Both need the Project's change permission and the
Script File's, because the request is scoped to a Project but writes declarations.

Script Files keep surfaces of their own for triage across projects: list, detail,
edit, filtering, global search, REST, and GraphQL. They carry no top-level navigation
item and no create, delete, bulk delete, bulk import or bulk edit route, because a
declaration is a Project setting. Over REST that is a method restriction, so POST and
DELETE answer 405.

The three discovery fields are readable and filterable everywhere, and writable
nowhere: no form, serializer, or GraphQL input accepts them, only project
validation writes them. `last_discovered_revision` is a nested revision in REST
and a revision relation in GraphQL. Revisions have read-only REST and GraphQL
surfaces of their own.

## Validation rules

A declaration is checked where it is made, so a path that could never run is
rejected up front rather than at validation time:

- The path is canonicalized to the same form project storage uses, and must
  satisfy the [source path policy](../configuration.md).
- The path must name an importable Python module: it ends in `.py`, every
  segment is a valid Python identifier and not a reserved keyword, and the
  project root `__init__.py` is refused because it names the package itself.
- Two script files of one project cannot collide when letter case is ignored, since
  hosts such as macOS treat `Utils.py` and `utils.py` as one file. The check
  covers every directory level, so `Lib/deploy.py` and `lib/audit.py` collide
  too, which is what the source tree they name would do at upload.
- Two script files of one project cannot import under one module name. `pkg.py` and
  `pkg/__init__.py` both name `pkg`, the package wins, and the other file would
  silently never execute.

## Invariants

| Invariant | Enforcement |
|---|---|
| One path is declared at most once per project | `unique_project_source_path` database constraint |
| The project cannot be changed | `clean()` for a per-field error, `save()` as the backstop for ORM writes |
| The source path cannot be changed | `clean()` and `save()`, compared after canonicalization so a re-spelling is not a change |
| The stored path is canonical | `save()` canonicalizes, so snapshots built straight from rows are safe |
| The stored path is importable | `save()` refuses an unimportable path, so it cannot freeze into a snapshot that activation could only reject |
| Discovery fields are system-managed | `editable=False`, only project validation writes them |
| `last_discovered_revision` belongs to the same project | `clean()` check |

Script Files are installation-global, like projects and revisions. Under NetBox
Branching they read and write the main schema from every branch, because a
branch-local script file set for an installation-global revision would let one
revision mean different things in different branches.

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must supply
canonical paths themselves.
