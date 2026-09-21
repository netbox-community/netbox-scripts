# Uploading Scripts

A Script Project whose source type is `upload` owns its content outright. This page
covers adding that content through the UI and what the plugin does with it.

## Creating a Project from one script

*Scripts > Projects*, then **Upload Script**. The form asks for five
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
3. A Script File is created for that path and enabled, so discovery imports the file.
4. A revision is staged: the content is written to project storage, verified against its
   manifest, and reaches `materialized`.
5. Validation is enqueued. It runs in a worker, imports the script file, and discovers the
   Scripts it publishes.
6. On a `valid` verdict, the revision is activated if you ticked **Activate this upload**, or if
   the Project's activation policy allows it.

Steps 5 and 6 need a running RQ worker. Without one the revision stays `materialized` and the
Project reports that new source is waiting to be validated.

### An uploaded file is always a script file

For this release, every uploaded `.py` file is declared as a script file. Uploading a dedicated
helper module is not supported, and a Project that bundles helpers alongside its script
files is managed through a Data Source rather than through uploads. The data model and the
package loader both support helper files already, so this is a restriction of the upload form
rather than of the engine.

Only Python source can be uploaded. Anything else is refused on the field, as are compiled
artifacts, because a compiled file imports without the source anyone would review.

### File names arrive flattened

A browser sends only the base name of an uploaded file, so `automation/deploy.py` arrives as
`deploy.py`. An upload therefore always names a file at the root of the Project, and it can
never create a nested script file. Nested paths reach a Project through its Data Source
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
flattening above visible rather than silent. A replacement never edits a revision in place, so the
previous one keeps its content and stays in the Project's history.

The tick is also asked for a path an upload has claimed but not finished storing. A browser
upload declares its script file while the request runs and writes the content once that request
commits, so for a moment the Project has the path without the bytes. Two uploads of one new name
can both start inside that moment, and without the tick the later one would replace the earlier
one's content with neither operator being asked. If you see it for a file you believe is new,
either another upload of that name is in flight or an earlier one failed to store.

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
bytes under an unchanged Script File selection resolves to the revision that already holds
them, verdict included, rather than creating a second one.

**A 201 returns a revision record, which does not mean the source was stored or is usable.** What the route
refuses outright it answers with a 400 naming `file`: a body over the per-file byte limit, a name
that is not a Python file, a path the Project already holds without `confirm_replace`, a path a
browser upload has declared but not finished storing, also without `confirm_replace`, a path that
collides with an existing declaration by letter case or by module name, and a Project whose source
is a Data Source rather than uploaded files. Everything else is accepted and answered with
a revision. Content the manifest will not accept, such as an upload
that trips a limit read over the whole tree rather than over one file, comes back as a revision
that is already `invalid` and carries no source digest, and a revision that fails to import or
publishes no Script reaches `invalid` a moment later. The verdict on the revision is the only
thing that says the source is usable.

Authorization is the same pair the **Add Script** page needs: the change permission on the
Project, because the Project exists and its source is being changed, and the add permission on
Script Files, because the upload declares its own script file. The Project's add
permission is not what authorizes this.

## Putting a revision in service

A revision that validates does not necessarily serve. The Project's **activation policy**
decides:

| Policy | Behaviour |
|---|---|
| Automatic if valid | Validation activates the revision itself once the verdict is `valid`. |
| Manual | The revision stops at `valid` and waits for an operator. |

For a manual Project, **Activate** on the Project's page names the revision that would go live
and puts it in service. It appears whenever an eligible revision other than the active one
exists, which includes a previously retired one, so a Project already serving its newest
revision still offers the button when it has something to roll back to. The confirmation names
the exact revision. Activating retires the previous revision in the same step.

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
digest, because a revision is identified by its source tree **and** the script file set it froze,
so changing the selection over unchanged source produces a second revision. The **Script Files**
column is what tells those two apart, and a revision's own page lists the paths it froze. The
**Script Files** tab shows which files discovery imports, with each declaration's discovery
outcome.

### Changing which files are script files

Saving the Script Files tab applies the new selection to the source the Project already holds.
Each revision freezes the enabled declarations at the moment it is staged, so the change needs
a revision of its own: the same stored content under the new script file configuration, which is
validated and then activated if the Project's activation policy allows. That work runs as a
background job, so the tab reports it is under way rather than showing the result.

Saving a selection that did not move stages nothing and queues nothing. A revision is
identified by its content together with its script file configuration, so the unchanged pair
resolves to the revision that already exists.

## Where the bytes go

Uploaded content is stored through the `netbox_scripts` entry of NetBox's `STORAGES` setting,
under a prefix naming the Project's storage key and the revision digest. That entry's backend can
keep it in local files or in object storage, and it is an entry of its own rather than the
`scripts` storage the built-in Custom Scripts use. See
[Project storage](configuration.md#project-storage) for the backend choice and
[Storage layout](models/scriptprojectrevision.md#storage-layout) for the key layout.

Identical content uploaded twice is stored once, because stored content is addressed by the
digest of its manifest. It resolves to the existing revision too while the Script File
selection is unchanged.
