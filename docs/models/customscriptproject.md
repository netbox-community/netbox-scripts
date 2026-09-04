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
| `active_revision` | `CustomScriptProjectRevision` | no | `on_delete=SET_NULL`, reverse name `active_revision_for`. Set only by the storage activation service |

Each project owns a history of immutable source snapshots, documented on the
[Custom Script Project Revision](customscriptprojectrevision.md) page. Its
declared entrypoints are [Custom Script Modules](customscriptmodule.md) and the
classes an activated revision publishes are [Custom Scripts](customscript.md).

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/projects/` |
| GraphQL | `custom_script_project` / `custom_script_project_list` |

A project's **Entrypoints** tab selects which of its source modules discovery
imports, and its detail page lists the [Custom Script
Modules](customscriptmodule.md) it has declared. The selection is also a REST
operation at `projects/<id>/entrypoints/`.

A project's **Files** tab lists the files of its current revision, one row per
manifest entry with its size and short checksum, and marks which paths are
enabled entrypoints. A declared path the revision does not hold is annotated,
and the annotation distinguishes one that is gone from the source from one a
newer revision holds that is not being served yet. The tab is empty until a
revision holds content.

## Invariants

| Invariant | Enforcement |
|---|---|
| `key` cannot change after creation | `clean()`, the edit form disables the field, REST returns 400 |
| `source_type` cannot change after creation, pending a dedicated source-transition workflow | `clean()`, the edit form disables the field, REST returns 400 |
| `storage_key` never changes | `save()` guard, the field is excluded from forms and read-only in REST |
| `data_path` is stored canonically: POSIX-style, relative, single separators, no leading `./` or trailing `/` | `clean()` and the REST serializer normalize. Absolute paths, `..` traversal, and backslashes are rejected |
| `data_source` projects require a `data_source`. An empty `data_path` roots the Project at the Data Source root and claims every file in the source | `clean()` plus the `enforce_source_ownership` database constraint |
| `upload` projects carry no `data_source` and no `data_path` | `clean()` plus the `enforce_source_ownership` database constraint |
| `active_revision` must belong to this project | `clean()` |
| A project whose active revision is deleted keeps serving nothing rather than blocking the delete | `SET_NULL` on `active_revision`, which is also what lets a project be deleted at all, since its revisions cascade |

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

## Activation policy

`activation_policy` decides whether a revision that passes validation goes into service on its
own:

| Value | Behaviour |
|---|---|
| `manual` | The revision stops at `valid`. An operator activates it from the project's page. |
| `automatic_if_valid` | The validation job activates the revision itself on a `valid` verdict. |

The [upload form](../uploading.md) asks for this separately from its **Activate this upload**
tick, because the two are different decisions: the tick puts that one revision into service,
while this field decides what later revisions do. The form leaves this on `manual`. An
automatic activation that is refused, for
example because the stored tree no longer matches its manifest, fails the validation job and
leaves both the verdict and the previously active revision alone.

For a Data Source-backed project the policy is what decides whether a synchronization changes
what the project serves, so it is the field to reach for when a repository should be tracked
but not trusted unattended. See [Data Source Projects](../data-sources.md).

Activating is always a choice between validated revisions, never a promotion of unvalidated
content. Retired revisions remain eligible, so returning to an earlier one is a matter of
selecting it.

## Identity notes

`storage_key` is the project's internal storage and runtime identity: it will
name physical storage, runtime packages, and cache paths. It is exposed
read-only for troubleshooting, but it is not the public identity of a project
or its scripts. Integrations must reference projects by `key` (or object ID),
never by `storage_key`.

## NetBox Branching

Custom Script Projects and their revisions are **installation-global**. A
revision's on-disk location is a pure function of the project's `storage_key`
and the revision's `digest`, with no branch or schema context, so one project
names exactly one source tree no matter which branch is active.

NetBox Branching's own `exempt_models` setting is the supported way to say so:

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'exempt_models': [
            'netbox_scripts.customscriptproject',
            'netbox_scripts.customscriptprojectrevision',
        ],
    },
}
```

List the models individually rather than using a `netbox_scripts.*`
wildcard. The wildcard is accurate today, because every model in this plugin is
installation-global, but it would also sweep in a model added later that is meant
to keep NetBox Branching's ordinary behaviour.

You do not have to configure this to be safe. The plugin also registers a
branching resolver so that a fresh installation already routes both models to the
main schema, which is the same treatment NetBox gives `core` objects. The
resolver is best effort, and the setting above is what an operator uses when it is
unavailable, so the two work together rather than competing.

What the plugin does not do is assume either one worked. Before it stages a
revision, activates one, or removes stored source, it asks NetBox Branching
through its public API whether these models are still routed to the main schema:

- If they are, the operation proceeds.
- If they are not, or the answer cannot be determined, **the storage operation is
  refused** and a system check reports the reason. The hint names both remedies,
  because the configuration above cannot resolve every case: where the routing
  cannot be inspected at all, the exemption is unverifiable too, and the fix is a
  NetBox Branching release that exposes the inspection API.
- **Deleting a project or a revision never removes source under an unresolved
  answer.** The rows go, the directory stays, and the skip is logged at error
  level. A leaked directory is recoverable by an operator or a future reconciler,
  whereas source deleted out from under a schema that still serves it is not.

NetBox itself keeps running throughout. An optional peer plugin whose routing
cannot be confirmed disables this plugin's storage operations, not the
installation.

Three consequences worth knowing before you rely on this:

- A project or revision created, changed, or deleted while a branch is active
  applies **immediately and globally**. It is not part of the branch's diff and
  is not replayed on merge or undone on revert.
- **Tags and journal entries on a project are branch-local.** A tag added or
  removed inside a branch, and a journal entry written inside a branch, belong
  to that branch and reach the main schema only when it merges. That is NetBox
  Branching's ordinary behaviour for tag assignments and journal entries, and
  it is what installation-global NetBox objects such as Data Sources already do.
  The project row itself, its revisions, and its source tree stay global
  throughout.
- Without this, a branch would hold its own rows pointing at the same bytes as
  main, and deleting a revision inside the branch would remove source that main
  still serves.

Only the two models above are installation-global. A model added to this plugin
later keeps NetBox Branching's ordinary behaviour, which is the right default for
one that owns no source tree. Two questions are worth asking when adding one:
whether it owns bytes on disk, in which case it belongs alongside the two above,
and whether it holds a concrete relation to a branch-aware model, which would
leave a row in the main schema pointing at a row that exists only inside a branch.

Whether script *execution* is branch-aware is a separate question and remains
an execution-model decision that lands with the execution work.

## Limitations

| Limitation | Impact |
|---|---|
| A project owns one source, never both kinds | `source_type` is immutable, so moving a project from uploads to a Data Source means creating a new one |
| No revision REST or GraphQL surface | Revisions are read-only history in the UI, so automation cannot stage or activate one |
| An entrypoint selection does not restage by itself | An upload project applies a changed selection when its next revision is staged. A Data Source-backed one applies it with **Reconcile Source** |
