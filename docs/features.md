# Features and limitations

NetBox Scripts lets you manage source, validate it and run Python Scripts in
NetBox. Use the guides below for everyday tasks and the model references for
fields, relationships and API details.

## From source to runnable Scripts

A [Project](reference/models/scriptproject.md) holds uploaded files or a Data
Source directory. Selecting [Script Files](reference/models/scriptfile.md)
chooses the modules used for discovery. Each
[revision](reference/models/scriptprojectrevision.md) captures the source and
selection together. Validation checks that snapshot before activation publishes
its runnable [Scripts](reference/models/netboxscript.md).

Each Project serves one active revision. Changing its source does not replace
that revision until activation succeeds. See the [main concepts](index.md#main-concepts)
for a short glossary and [Runtime and loading](reference/runtime.md) for validation
and import details. Private import names avoid naming conflicts, but are not a
sandbox. Source runs with the permissions of the process loading it.

## Run and monitor Scripts

Browse, search and filter published Scripts, complete their forms, and run them
now or on a schedule. Choose whether to keep database changes or use a dry run.
A dry run does not undo external actions. See
[Running Scripts](user-guide/execution.md).

One-shot runs use the revision pinned when requested. Recurring runs use the
active revision at each occurrence. Each run records its status, revision, log
and output on a Job. The Script's **Jobs** tab shows the history NetBox retains,
including history for a Script that has been retired.

### Execution defaults

Administrators can enable or disable Scripts individually or in bulk, and
set overrides for their timeout, notification policy and commit default through
the UI or REST. Unset overrides follow class defaults. Each recurring successor
resolves its timeout from the current Script configuration. See
[Override execution defaults](user-guide/execution.md#override-execution-defaults).

## Manage source

Create, edit, delete, bulk-import and tag Projects.
[Upload a Python file](administration/uploading.md) to create a Project or add
to one, or [connect a Data Source directory](administration/data-sources.md)
for source that includes helpers and resources. Data Source Projects reconcile
after synchronization or through **Reconcile Source**, using NetBox's current
file inventory.

Select Script Files in the UI or over REST. Inspect validation results and errors,
then activate an eligible revision manually or let the Project's policy activate
it. **Source state** explains the latest progress. The **Revisions** and
**Revision Files** tabs show history, file sizes, checksums and missing or newer
paths. **Repair Scripts** republishes rows from the serving revision and reports
how many changed. See [Review source and revision status](administration/uploading.md#review-source-and-revision-status)
and [activation](reference/runtime.md#what-activation-does).

For existing built-in Custom Scripts, the [migration workflow](administration/migration.md)
starts with inventory and staging without activation, followed by cutover.
Read its alpha warnings and recovery requirements before beginning.

## Write Scripts

Script authors use the [authoring API](user-guide/authoring.md) to define classes,
variables, forms and logging. Select the publishing files and use the
[discovery rules](user-guide/authoring.md#publishing-scripts-from-a-project)
to control which classes appear and their order.

## Integrate with NetBox

Use REST and GraphQL to filter Projects and revision history, including by
Project, revision status or digest. GraphQL filters include typed choices.
List views, filtering, tags, custom fields, change logging and search vary by
object. Check the [model references](reference/models/scriptproject.md) for each
object's available interfaces.

[Event Rules](administration/event-rules.md) can run Scripts using their action
data. They can also react to supported object changes and send webhooks. The
[delivery conditions](administration/event-rules.md#scripts-as-event-sources)
explain which operations produce those events.

## Limitations

| Area | What that means |
|---|---|
| Uploading helper modules, archives, and other resources | Uploads accept one Script File at a time. Use a Data Source for helper files and resources. |
| A repository manifest declaring its own script files | Select Script Files on the Project in NetBox. |
| Declared pip requirements | Revision dependencies are not read or installed. |
| Recorded input values | Jobs record the Script, revision and result, but not submitted inputs. |
| Recurring runs carrying an upload | An uploaded file can be used for a one-shot run, not a recurrence. |
| Legacy Reports | Classes with `test_*` methods and no `run()` are rejected. Rewrite them as Scripts with a `run()` method. |
| A permanent `extras.scripts` layer | Legacy imports are transitional and are expected to stop working with NetBox v5.0. Move to the plugin's authoring API before that upgrade. |
