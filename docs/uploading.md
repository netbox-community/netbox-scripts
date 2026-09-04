# Uploading Scripts

A Custom Script Project whose source type is `upload` owns its content outright. This page
covers adding that content through the UI and what the plugin does with it.

## Creating a Project from one script

*Custom Scripts > Custom Script Projects*, then **Upload Script**. The form asks for five
things and nothing else:

| Field | Meaning |
|---|---|
| Name | The Project's display name. |
| Key | Its stable identifier, generated from the name and immutable afterwards. |
| Script | A Python module to publish. |
| Activate this upload | On by default. Puts **this one** revision in service as soon as it validates. |
| Activation policy | What later revisions do, and this one too when the tick above is clear. Manual by default, so nothing activates unasked. |

Nothing about paths, digests, revision status, or storage locations is asked for, because the
plugin derives all of it. What happens when the form is submitted:

1. The Project is created with source type `upload`.
2. The uploaded file's name becomes its path within the Project, canonicalized under the
   [source path policy](configuration.md#source-path-policy).
3. A Custom Script Module is created for that path and enabled, so the file is an entrypoint.
4. A revision is staged: the content is written to project storage, verified against its
   manifest, and reaches `materialized`.
5. Validation is enqueued. It runs in a worker, imports the entrypoint, and discovers the
   Custom Scripts it publishes.
6. On a `valid` verdict, the revision is activated if you ticked **Activate this upload**, or if
   the Project's activation policy allows it.

Steps 5 and 6 need a running RQ worker. Without one the revision stays `materialized` and the
Project reports that new source is waiting to be validated.

### An uploaded file is always an entrypoint

For this release, every uploaded `.py` file is declared as an entrypoint. Uploading a dedicated
helper module is not supported, and a Project that bundles helpers alongside its executable
modules is managed through a Data Source rather than through uploads. The data model and the
package loader both support helper files already, so this is a restriction of the upload form
rather than of the engine.

Only Python source can be uploaded. Anything else is refused on the field, as are compiled
artifacts, because a compiled file imports without the source anyone would review.

### File names arrive flattened

A browser sends only the base name of an uploaded file, so `automation/deploy.py` arrives as
`deploy.py`. An upload therefore always names a file at the root of the Project, and it can
never create a nested entrypoint. Nested paths reach a Project through its Data Source
directory instead.

The consequence worth knowing: two files you think of as different, `automation/deploy.py` and
`audit/deploy.py`, are the same path once uploaded to one Project. The second is a replacement
of the first, and the form asks before doing it.

The REST route flattens too, and it does so by the plugin's own rule rather than by relying on
what a parser happens to hand over. A path in a REST upload is client-local structure, so no
destination is taken from the request at all. Supplying a whole tree, with the paths preserved,
is a separate contract rather than a gap here.

## Adding more scripts to a Project

**Add Script** on the Project's page uploads another file. A revision is a whole tree, so the
new revision holds everything the Project already had plus the new file, and the new path is
declared and enabled automatically.

Re-uploading a path the Project already holds needs the **Replace the existing file** tick.
That check compares the canonical path, not the name you picked, which is what makes the
flattening above visible rather than silent. The replacement produces a new revision, so the
previous one keeps its content and stays in the Project's history.

A name that collides with an existing file only by letter case, `Deploy.py` against
`deploy.py`, is refused rather than replaced. Two such paths cannot both be materialized on a
case-insensitive filesystem, so the collision is rejected at the point it is introduced.

### Over REST

The same operation without the browser, for a pipeline that generates or vendors scripts:

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

It accepts one Python file and returns the revision it staged, so the caller can poll that
revision until it reaches a verdict. Add `-F confirm_replace=true` to replace a path the
Project already holds, which is the same confirmation the form asks for. Uploading identical
bytes resolves to the revision that already holds them, verdict included, rather than creating
a second one.

Authorization is the same pair the **Add Script** page needs: the change permission on the
Project, because the Project exists and its source is being changed, and the add permission on
Custom Script Modules, because the upload declares its own entrypoint. The Project's add
permission is not what authorizes this.

## Putting a revision in service

A revision that validates does not necessarily serve. The Project's **activation policy**
decides:

| Policy | Behaviour |
|---|---|
| Automatic if valid | Validation activates the revision itself once the verdict is `valid`. |
| Manual | The revision stops at `valid` and waits for an operator. |

For a manual Project, **Activate** on the Project's page names the revision that would go live
and puts it in service. It appears only when there is something to activate, so a Project
already serving its newest revision shows no button. Activating retires the previous revision
in the same step.

Activation re-verifies the stored tree before moving the pointer, so a revision whose content
no longer matches its manifest is refused and the Project keeps serving what it served before.

## Following what happened

The Project's page carries two readings, deliberately kept apart. **Source state**, on the
**Project** panel, is a plain-language summary of where the newest source stands, and it is the
part that explains a Project not yet serving what was last uploaded, for example that new source
is being validated or that it failed validation. The **Current revision** panel describes the
revision whose tree is the Project's source, which is the active one or, before anything has
been activated, the newest one that was stored. It carries the date, status, digest, file
count, size, and activation time. The two panels can describe different revisions, which is
why they are not stacked in one.

The **Revisions** tab lists every revision the Project has ever had. Two rows can show the same
digest, because a revision is identified by its source tree **and** the entrypoint set it froze,
so changing the selection over unchanged source produces a second revision. The **Entrypoints**
column is what tells those two apart, and a revision's own page lists the paths it froze. The
**Entrypoints** tab shows which modules discovery imports, with each declaration's discovery
outcome.

### Changing which modules are entrypoints

Saving the Entrypoints tab applies the new selection to the source the Project already holds.
Each revision freezes the enabled declarations at the moment it is staged, so the change needs
a revision of its own: the same stored content under the new entrypoint configuration, which is
validated and then activated if the Project's activation policy allows. That work runs as a
background job, so the tab reports it is under way rather than showing the result.

Saving a selection that did not move stages nothing and queues nothing. A revision is
identified by its content together with its entrypoint configuration, so the unchanged pair
resolves to the revision that already exists.

## Where the bytes go

Uploaded content is stored through the `netbox_scripts` entry of NetBox's `STORAGES`
setting, never on the local filesystem, under a prefix naming the Project's storage key and the
revision digest. See [Project storage](configuration.md#project-storage) for the backend
choice and [Storage layout](models/customscriptprojectrevision.md#storage-layout) for the key
layout.

Identical content uploaded twice resolves to the existing revision rather than storing a second
copy, because a revision is addressed by the digest of its manifest.
