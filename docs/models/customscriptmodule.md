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
| `discovery_status` | choice | system | `pending`, `discovered`, or `failed`, describing the most recent validation of the current declaration |
| `discovery_error` | text | system | Why discovery failed, empty otherwise |
| `last_discovered_revision` | FK | system | The revision whose validation last wrote these discovery fields |

The three discovery fields are system-managed. Project validation writes them,
no form or serializer accepts them, and they describe the current declaration:
a module renamed after a validation is a different declaration, so its results
start over at `pending`.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `project` | `CustomScriptProject` | yes | `on_delete=CASCADE`, reverse name `modules` |
| `last_discovered_revision` | `CustomScriptProjectRevision` | no | `on_delete=SET_NULL`, no reverse accessor |

## API

| Surface | Endpoint or field |
|---|---|
| REST | none in this release |
| GraphQL | none in this release |

Modules carry no UI, REST, GraphQL, filterset, or global-search surface yet.
The full object surface arrives in a follow-up release, in this one rows are
created from code, for example through `manage.py nbshell`.

## Validation rules

A declaration is checked where it is made, so a path that could never run is
rejected up front rather than at validation time:

- The path is canonicalized to the same form project storage uses, and must
  satisfy the [source path policy](../configuration.md).
- The path must name an importable Python module: it ends in `.py`, every
  segment is a valid Python identifier and not a reserved keyword, and the
  project root `__init__.py` is refused because it names the package itself.
- Two modules of one project cannot collide when letter case is ignored, since
  hosts such as macOS treat `Utils.py` and `utils.py` as one file.

## Invariants

| Invariant | Enforcement |
|---|---|
| One path is declared at most once per project | `unique_project_source_path` database constraint |
| The stored path is canonical | `save()` canonicalizes, so snapshots built straight from rows are safe |
| Discovery fields are system-managed | `editable=False`, only project validation writes them |
| `last_discovered_revision` belongs to the same project | `clean()` check |

Modules are installation-global, like projects and revisions. Under NetBox
Branching they read and write the main schema from every branch, because a
branch-local entrypoint set for an installation-global revision would let one
revision mean different things in different branches.

Code paths that bypass validation (`QuerySet.update()`, raw SQL) must supply
canonical paths themselves.
