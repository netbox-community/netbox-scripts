# Custom Script Module

A Custom Script Module declares one executable entrypoint within a Custom
Script Project: a Python file of the project's source tree that discovery
imports and publishes Custom Scripts from. Helper files need no module row,
they stay importable by the entrypoints without being one, so a module list is
the project's explicit statement of what runs rather than an inventory of every
file.

Enabled module declarations are frozen into each revision at staging time as
that revision's [entrypoint snapshot](customscriptprojectrevision.md). Editing,
disabling, or deleting a module therefore changes future revisions and never
what an existing revision was validated against.

## Fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `project` | FK | yes | Owning Custom Script Project |
| `source_path` | string | yes | POSIX-style relative path of the entrypoint Python file, stored in canonical form |
| `enabled` | boolean | yes | Whether staging includes this declaration in new revision snapshots, default true |
| `discovery_status` | choice | system | `pending`, `discovered`, `no_scripts`, or `failed`, describing the most recent validation of the current declaration |
| `discovery_error` | text | system | Why discovery failed, or why an entrypoint published nothing, empty otherwise |
| `last_discovered_revision` | FK | system | The revision whose validation last wrote these discovery fields |

The three discovery fields are system-managed. Project validation writes them,
no form or serializer accepts them, and they describe the current declaration.

A module has no name of its own. It is identified by its project and its source
path, and both are frozen once the module exists, so a declaration can never be
repointed at a different file. That keeps the discovery fields honest: a
repointed row would report a verdict for a file it no longer names.

## Renaming or moving an entrypoint

There is no rename. When a file moves or is renamed in the project's source, the
new path appears as a candidate on the Entrypoints tab and the old one is
reported as missing. Tick the new path and untick the old one:

1. Ticking the new path declares it, starting at `pending` until the next validation.
2. Unticking the old path clears its `enabled`, which is what selection means.

Unticking disables rather than deletes, which keeps the old declaration's
discovery history and keeps its path reserved, because
`unique_project_source_path` does not consider `enabled`. Renaming the file back
therefore re-enables the original row instead of colliding with it. Staging includes only enabled declarations, so
a disabled one stops reaching new revisions immediately while the revisions it
was already snapshotted into keep meaning what they meant.

Delete a module only to discard its history, for example when the declaration
was a mistake that never validated.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `CustomScriptProject` | yes | `on_delete=CASCADE`, reverse name `modules` |
| `last_discovered_revision` | `CustomScriptProjectRevision` | no | `on_delete=SET_NULL`, no reverse accessor |

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/custom-scripts/modules/` |
| GraphQL | `custom_script_module` / `custom_script_module_list` |

Entrypoints are selected on the owning Project's **Entrypoints** tab, which
lists the importable modules of its source at any depth and never asks for a
typed path. The same operation is available over REST:

```text
GET  /api/plugins/custom-scripts/projects/<id>/entrypoints/
PUT  /api/plugins/custom-scripts/projects/<id>/entrypoints/   {"paths": [...]}
```

`GET` reports every candidate with `selected`, `available`, and its
`discovery_status`. `PUT` replaces the selection, refusing any path the project
has no source file at. Both need the Project's change permission and the
Module's, because the request is scoped to a Project but writes declarations.

Modules keep read surfaces of their own for triage across projects: list,
detail, filtering, global search, REST, and GraphQL. They carry no top-level
navigation item, no bulk import, and no bulk edit, because a declaration is a
Project setting rather than an object managed in bulk.

The three discovery fields are readable and filterable everywhere, and writable
nowhere: no form, serializer, or GraphQL input accepts them, only project
validation writes them. `last_discovered_revision` appears in REST as a plain
ID, and is absent from GraphQL, because revisions carry no object surface of
their own in this release.

## Validation rules

A declaration is checked where it is made, so a path that could never run is
rejected up front rather than at validation time:

- The path is canonicalized to the same form project storage uses, and must
  satisfy the [source path policy](../configuration.md).
- The path must name an importable Python module: it ends in `.py`, every
  segment is a valid Python identifier and not a reserved keyword, and the
  project root `__init__.py` is refused because it names the package itself.
- Two modules of one project cannot collide when letter case is ignored, since
  hosts such as macOS treat `Utils.py` and `utils.py` as one file. The check
  covers every directory level, so `Lib/deploy.py` and `lib/audit.py` collide
  too, which is what the source tree they name would do at upload.
- Two modules of one project cannot import under one module name. `pkg.py` and
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

Modules are installation-global, like projects and revisions. Under NetBox
Branching they read and write the main schema from every branch, because a
branch-local entrypoint set for an installation-global revision would let one
revision mean different things in different branches.

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must supply
canonical paths themselves.
