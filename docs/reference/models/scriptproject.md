# Script Project

A Script Project owns one complete source tree and the settings for its Scripts.
The tree also forms the Python package boundary used when loading and executing
those Scripts.

## Fields

FK means foreign key. Fields marked `auto` are assigned or maintained by the
plugin rather than supplied as editable configuration.

| Field | Type | Required | Notes |
|---|---|---|---|
| `name` | string | yes | Display name. Can be changed |
| `key` | slug | yes | Unique public identifier. Cannot change after creation |
| `storage_key` | UUID | auto | Internal storage and runtime identity, assigned on creation and immutable |
| `source_type` | choice | yes | `upload` (default) or `data_source`. Cannot change after creation |
| `data_source` | FK | conditional | Required for a Data Source Project. Not allowed for uploads |
| `data_path` | string | conditional | Required for a Data Source Project. Names a directory within the Data Source, not its root. Not allowed for uploads |
| `activation_policy` | choice | yes | `manual` (default) or `automatic_if_valid` |
| `active_revision` | FK | auto | Revision currently served by the Project. Managed by activation and deactivation |
| `enabled` | boolean | yes | Whether Scripts in this Project may run. Defaults to `true` |
| `description` | string | no | Short description of the Project |
| `comments` | text | no | Operational notes |

Use `enabled` to control execution. Use activation and deactivation to choose
which revision the Project serves. These are separate controls.

## Relationships

| Relationship | Target | Required | Notes |
|---|---|---|---|
| `data_source` | `core.DataSource` | conditional | `on_delete=PROTECT`, no reverse relation. Set only for `source_type=data_source` |
| `revisions` | `ScriptProjectRevision` | no | Reverse of the revision's `project`, with `on_delete=CASCADE`. Revisions recorded for this Project |
| `active_revision` | `ScriptProjectRevision` | no | `on_delete=SET_NULL`, reverse name `active_revision_for`. Managed by activation and deactivation |

A Project's immutable source snapshots are
[Script Project Revisions](scriptprojectrevision.md). Its declared source
modules are [Script Files](scriptfile.md), and the classes published by
activation are [Scripts](netboxscript.md).

## API

| Surface | Endpoint or field |
|---|---|
| REST | `/api/plugins/netbox-scripts/projects/` |
| GraphQL | `netbox_script_project` / `netbox_script_project_list` |

The **Script Files** tab selects the source modules used for discovery. The
Project's detail page lists its declarations. REST provides the same selection
operation at `projects/<id>/script-files/`. See [Script File](scriptfile.md#api)
for its request format and permissions.

The **Revision Files** tab lists each file in the current revision, with its
size, short checksum and enabled Script File status. It identifies the revision
being displayed and links to the selection tab. A missing declared path is
labelled to distinguish a file absent from the source from one waiting in a
newer, inactive revision. The tab is empty until a revision contains stored
source.

## Activation policy

`activation_policy` controls whether a validated revision activates
automatically or waits for an operator:

| Value | Behaviour |
|---|---|
| `manual` | The revision remains `valid` until an operator activates it. A one-off upload activation request is separate |
| `automatic_if_valid` | An eligible revision activates automatically once it has a `valid` verdict |

On the [upload form](../../administration/uploading.md), **Activate this upload** requests
activation for that upload without changing the policy for later revisions.
When the checkbox is clear, the activation policy still applies. The form
defaults to `manual`.

If automatic activation is refused, for example because stored content no
longer matches its manifest, the validation Job fails. The validation verdict
and the previously active revision remain unchanged.

For a Data Source Project, choose `manual` to review synchronized changes before
serving them. Choose `automatic_if_valid` to activate eligible validated changes
automatically. See [Data Source Projects](../../administration/data-sources.md).

Only validated revisions can be activated. A `retired` revision remains
eligible for explicit activation, allowing an operator to return to earlier
source. Automatic activation also checks the accepted source and selection, as
[described below](#limitations).

## Invariants

| Invariant | Enforcement |
|---|---|
| `key` cannot change after creation | `clean()`, disabled edit-form field, HTTP 400 on REST changes |
| `source_type` cannot change after creation | `clean()`, disabled edit-form field, HTTP 400 on REST changes. No source-transition workflow is provided |
| `storage_key` never changes | `save()` guard, excluded from forms and read-only in REST |
| `data_path` is canonical: POSIX-style, relative, single separators, no leading `./` or trailing `/` | `clean()` and the REST serializer normalize it. Absolute paths, `..` traversal and backslashes are rejected |
| `data_source` Projects require a `data_source` and non-empty `data_path` | `clean()` and the `enforce_source_ownership` database constraint |
| `upload` Projects have no `data_source` or `data_path` | `clean()` and the `enforce_source_ownership` database constraint |
| Changing `activation_policy`, `data_source` or `data_path` requires both `activate` and `change` | Object-scoped authorization against the stored Project, followed by the locked `save()` guard. See the write rules below |
| `active_revision` belongs to this Project | `clean()` |
| Only activation and deactivation write `active_revision` | A full `save()` restores the stored pointer. The services include it explicitly in `update_fields` |
| Removing the active revision clears the Project's pointer | `SET_NULL` on `active_revision` allows the Project's revision cascade to complete |

**Source-setting writes.** The serializer's `validate()`, the edit form's
`clean()` and both bulk views' save hooks check activation permission against
the stored Project. Project creation and unchanged submitted values are exempt
from this additional check. The locked `save()` guard rejects unauthorized or
intervening source-setting changes rather than overwriting them from a stale
instance. See [Permissions](../../administration/permissions.md#script-project).

Code that bypasses validation, such as `QuerySet.update()` or raw SQL, must
supply canonical values. The database constraint enforces source ownership, not
path spelling.

Deleting a Project cascades to its revisions. Their stored identities and
manifests are captured before deletion so background cleanup can reclaim their
source. The deleting process queues cleanup rather than removing stored files
itself. An untrusted manifest refuses the deletion, preserving the row and its
cleanup inventory.

## Identity notes

Use `key` or the object ID when integrating with a Project. `storage_key` is
read-only and exposed for troubleshooting, not as a public identifier. It names
stored content, runtime packages and cache paths and never changes.

## NetBox Branching

All five plugin models are **installation-global**. A revision's storage path
uses its Project's `storage_key` and its own `digest`, with no branch or schema
component. The same revision therefore names the same stored content in every
branch. See [NetBox Branching](../../administration/branching.md) for the
exemption setting, the routing check and the execution policy.

**For developers adding models:** retain ordinary NetBox Branching behavior
unless the model represents installation-wide content. Check both its scope
and its relationships. A main-schema row must not depend on a related row that
exists only in a branch. Global source identities also must not be duplicated
as branch-local rows, where deletion could remove content still served by main.

## Limitations

| Limitation | Impact |
|---|---|
| A Project uses one source type | `source_type` is immutable. Moving between uploads and a Data Source requires a new Project |
| Revisions cannot be edited directly | REST and GraphQL expose read-only history. Source actions stage revisions, and activation remains a Project action |

Saving a changed Script File selection queues a refresh of the accepted stored
source. Saving the single preselected candidate for the first time also counts
as a change. A Data Source Project with no stored revision still needs its first
reconciliation.

**Accepted source and active source are separate.** A repeated upload or
selection can reuse an older immutable revision and make it the accepted source
again. Later uploads build on that accepted source, not the newest row by
creation time.

Automatic activation checks that the validated revision is still the accepted
source and that its declaration snapshot matches the current selection.
Explicit historical activation does not change the source base for later
uploads.
