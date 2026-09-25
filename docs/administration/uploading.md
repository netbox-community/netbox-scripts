# Uploading Scripts

Use the UI or REST API to add Python source to an upload-based Project. The
Project stores changes as revisions.

## Creating a Project from one script

Open *Scripts > Projects* and choose **Upload Script**.

| Field | Meaning |
|---|---|
| Name | The Project's display name. |
| Key | Its stable identifier, generated from the name and fixed after creation. |
| Script | The Python module to publish. |
| Activate this upload | Selected by default. Activates this revision after successful validation. |
| Activation policy | Controls later revisions and this upload when **Activate this upload** is cleared. Defaults to Manual. |

The plugin determines paths, digests, revision status and storage locations.
When you submit the form:

1. It creates a Project with source type `upload`.
2. It normalizes the filename as a Project-relative path using the
   [source path policy](configuration.md#source-path-policy).
3. It creates and enables a Script File declaration for discovery.
4. It stages the source in Project storage, verifies it against the manifest,
   and records a `materialized` revision.
5. It queues validation to import the Script File and discover its Scripts.
6. After a `valid` result, it activates the revision when **Activate this upload**
   is selected or the Project's policy permits it.

Validation and the following automatic activation require an RQ worker. Without
one, the revision remains `materialized` and Source state reports that it is
waiting for validation.

### An uploaded file is always a script file

Every uploaded `.py` file is declared as a Script File. This release does not
support dedicated helper uploads. Use a Data Source Project for source that
includes helpers or other resources. The loader supports these files, but the
upload interface does not accept them separately.

Only Python source is accepted. Other file types and compiled artifacts are
rejected on the upload field. Bytecode without reviewable source is not supported.

### File names arrive flattened

UI and REST uploads use only the base filename. For example,
`automation/deploy.py` becomes `deploy.py` at the Project root. Use a Data Source
to preserve nested paths.

Files such as `automation/deploy.py` and `audit/deploy.py` therefore have the same
destination in one upload-based Project. Uploading the second requires replacement
confirmation. Paths on the caller's machine do not become destination directories.

## Adding more scripts to a Project

Choose **Add Script** on the Project's page. The new revision includes the existing
tree and the uploaded file. A new path is declared and enabled automatically.

To replace a stored path, select **Replace the existing file**. The check uses its
normalized path, including the flattened filename. Replacement creates a revision
rather than editing old source. Earlier revisions keep their content and history.

Confirmation is also required for a path already declared by an upload whose
content is not yet stored. You may see it while another upload is in progress
or after an earlier one failed. This prevents overlapping uploads from silently
replacing each other's content.

Case-only collisions, such as `Deploy.py` and `deploy.py`, are rejected rather
than replaced. Those paths cannot coexist reliably on case-insensitive filesystems.

### Over REST

Upload a single Python file to an existing Project:

```
POST /api/plugins/netbox-scripts/projects/42/upload/
Authorization: Token $NETBOX_TOKEN
Content-Type: multipart/form-data
```

```bash
curl -sS -X POST \
  -H "Authorization: Token $NETBOX_TOKEN" \
  -F file=@deploy.py \
  https://netbox.example.com/api/plugins/netbox-scripts/projects/42/upload/
```

Add `-F confirm_replace=true` to replace an existing or reserved path. The endpoint
returns a revision you can poll for its validation result. Identical source and
an unchanged Script File selection reuse an existing revision rather than create
a duplicate.

**HTTP 201 returns a revision record. It does not guarantee successful storage
or validation.** Inspect the revision's status and errors.

The endpoint returns HTTP 400 naming `file` for a file over the per-file limit,
a non-Python filename, missing replacement confirmation, a case or module-name
collision, or an attempt to upload to a Data Source Project. Confirmation is
required for paths declared by an unfinished browser upload as well as stored paths.

Other content problems are reported on the revision. A whole-tree limit or other
manifest rejection can return an `invalid` revision without a source digest.
Import failures or source that publishes no Script can become `invalid` during
later validation. The revision's result determines whether it is usable.

This action requires `change` permission on the Project and `add` permission on
Script Files, like the **Add Script** page. Project `add` permission does not
authorize uploads to an existing Project.

## Putting a revision in service

A valid revision is activated according to the Project's policy:

| Policy | Behaviour |
|---|---|
| Automatic if valid | Activates the revision after successful validation. |
| Manual | Leaves the revision `valid` until an operator activates it. |

For manual activation, choose **Activate** on the Project's page. The confirmation
names the exact revision. The action appears when an eligible revision other than
the active one exists, including a retired revision available for rollback.
Activating another revision retires the previous active one in the same step.

Activation verifies stored source again. If it no longer matches the manifest,
activation is refused and the current revision remains in service.

## Following what happened

**Source state**, on the **Project** panel, explains what is happening to the newest
source, such as waiting for validation or failing it.

**Current revision** describes the active revision or, before the first activation,
the newest stored revision. It shows the date, status, digest, file count, size and
activation time. The two panels may describe different revisions when newer source
is not active yet.

The **Revisions** tab lists revision history. Rows can share a content digest but
have different Script File selections. The **Script Files** column distinguishes
these snapshots, and each revision's page lists its selected paths. The Project's
**Script Files** tab shows declarations and their discovery results.

### Changing which files are script files

Saving a changed Script File selection applies it to the source the Project
already holds. The revision captures that selection, is validated, and is activated
if the Project's policy allows. This work runs in the background, so the tab reports
that it is in progress rather than showing a final result immediately.

Saving an unchanged selection stages and queues nothing. Revision identity combines
source content with the selected Script Files.

## Where the bytes go

Uploads use the `netbox_scripts` entry in NetBox's `STORAGES` setting. The key prefix
contains the Project's storage key and revision digest. The backend can use local
files or object storage. It is separate from the `scripts` storage entry used by
built-in Custom Scripts.

See [Project storage](configuration.md#project-storage) for backend choices and
[Storage layout](../reference/models/scriptprojectrevision.md#storage-layout) for the key format.

Identical content is stored once, using its manifest digest. It also resolves to
the same revision when the Script File selection is unchanged.
