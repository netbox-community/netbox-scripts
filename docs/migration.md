# Migration

An installation that already uses NetBox's built-in Custom Scripts can have that content read,
reported on, and staged as Custom Script Projects without anything changing hands. This page
covers the two passes that do it, how to read what they report, and what they deliberately leave
alone.

Nothing on this page activates a Project or alters what the built-in feature serves. For the whole
of this release the built-in implementation stays authoritative, and the two run side by side.

## What the two passes do

| Pass | What it does |
|---|---|
| Inventory | Reads every built-in script module, classifies its authoring dialect, works out which Projects a migration would create, and counts the Event Rules, permissions and Jobs a migration would touch. Writes nothing. |
| Staging | Creates those Projects, declares their entrypoints, and stages their content as revisions. Activates nothing. |

Both run as background jobs and record what they found on their own Job row, so the result stays
readable after the run. Run the inventory first and act on what it reports. Staging refuses to run
at all while anything blocks.

## Starting a pass

Neither pass has a button yet. On a self-hosted installation, start one from NetBox's shell:

```python
from netbox_custom_scripts.jobs import MigrationInventoryJob, MigrationStagingJob

MigrationInventoryJob.enqueue()
MigrationStagingJob.enqueue()
```

Each call returns the Job it queued, and *Core > Jobs* shows its log and its recorded result.

NetBox Cloud and NetBox Enterprise cannot run a shell or a management command, so on those
platforms both passes wait for the migration interface in a later release. Until then this page
describes what the passes will do rather than something you can start today.

## Reading the inventory

The report lands on the Job's data. It carries six keys.

| Key | What it holds |
|---|---|
| `status` | `ready`, `warning` or `blocking`, whichever is the worst level any finding reached. |
| `modules` | One entry per built-in script module: its path, its file root, its authoring dialect, and the script classes it publishes today. |
| `projects` | The Projects a migration would create, each with its key, name, source type and the modules it would hold. |
| `dialects` | How many modules fell into each dialect. |
| `references` | How many Event Rules, permissions and Jobs point at the built-in feature. |
| `findings` | Everything an operator has to act on, each with a level, a code, the module it belongs to and a message. |

The reference counts are the size of the cutover, not of this migration. Nothing here rewrites an
Event Rule, a permission or a Job.

## What the three statuses mean

| Status | Meaning |
|---|---|
| `ready` | Every module is already written against this plugin's authoring API. Staging can run. |
| `warning` | Staging can run, and there is work to do before NetBox v5.0. |
| `blocking` | Staging refuses. Something in the source could never be imported, or needs a rewrite. |

A `warning` is almost always the `legacy_import` finding: the module imports its authoring API from
`extras.scripts`. That import works here today and stops working at NetBox v5.0, so **the
legacy-import list is the work queue to clear before that upgrade**. It is what turns v5.0 into a
deadline rather than a cliff. See [Authoring](authoring.md) for the forms that resolve and for why
the compatibility layer is transitional.

The blocking findings are these.

| Code | Why it blocks |
|---|---|
| `report_style` | The class declares `test_` methods and no `run()`. A report needs a rewrite, at any NetBox version, so it is reported apart from the legacy-import list rather than inside it. |
| `not_importable` | The file name is not a valid Python identifier, so no loader could ever import it. A hyphenated name is the common case. Rename the file in the source. |
| `unparsable` | The stored source is not valid Python. |
| `source_unreadable` | The stored bytes could not be read at all. A legacy Report whose content sits outside the scripts storage root reports this. |

One unreadable or unparsable module never stops the inventory. It becomes a finding, and the rest
of the report is still produced.

## What grouping produces

A Project is one source tree and one Python package boundary, so a migration has to decide which
legacy modules belong together. The rule reads the path each module already records.

| Source | Result |
|---|---|
| Backed by a Data Source | One Project per folder that holds scripts, keyed by the Data Source and that folder. |
| Uploaded | One Project per module, holding that single file. |

A folder inside another script-holding folder joins the one above it rather than becoming a second
Project. A Project's tree already contains its subdirectories, so the higher Project holds the
deeper file either way, and two Projects would hold the same content twice.

One consequence is worth knowing, because it is a gain rather than a compromise. The built-in
feature stores a synchronized file under its base name and never brings a sibling with it, so a
script has no way to share code with a helper module. A migrated Project holds the **whole folder**,
so the helpers beside a script arrive with it and a relative import between them resolves.

## Staging

Staging creates each proposed Project and stages its content. It can be run as many times as you
like: identity is derived from the source rather than recorded in bookkeeping, so a Data Source
Project resolves to the one already covering that folder, an uploaded one to its own key, and
identical content resolves to the revision that already holds it. A second pass creates nothing.

Every Project it creates takes the **manual** activation policy, whatever you might choose for it
later. So each staged revision reaches its `valid` verdict and then waits. Putting one in service is
the separate, deliberate step described under
[Putting a revision in service](data-sources.md#putting-a-revision-in-service).

If the report is `blocking`, staging logs every blocking finding and stops without creating
anything. Fix the source, run the inventory again, and stage once it is clear.

## What is not part of this release

| Area | Status |
|---|---|
| Activating a staged Project | Planned. Staging reaches a verdict and waits, and an operator activates. |
| Repointing Event Rules, permissions and Job history | Planned, with the cutover. The inventory counts them so the size is known. |
| Retiring the built-in scripts | Planned, with the cutover. They keep running throughout. |
| Starting either pass from the interface | Planned. See [Starting a pass](#starting-a-pass). |
| Choosing a different grouping | Not planned. Edit the staged Projects afterwards if you want a different shape. |
