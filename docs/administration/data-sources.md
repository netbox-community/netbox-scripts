# Data Source Projects

Connect a Project to one directory in a NetBox Data Source. Synchronization
copies that directory into revisions. You choose which Python files are selected
for Script discovery.

## Pointing a Project at a directory

Open *Scripts > Projects*, choose **Add**, and select `Data source` as the source
type. This reveals the source and path fields.

| Field | Meaning |
|---|---|
| Data source | The NetBox Data Source holding the files. |
| Data path | The directory whose contents form the Project's source. |
| Activation policy | Whether valid revisions activate automatically or wait for an operator. |

Use a non-empty directory path relative to the Data Source root, without a leading
slash or traversal segments. The Data Source root itself cannot be used. Paths are
matched by segment, so `automation/netbox` does not include `automation/netbox-old`.

Projects on the same Data Source must have separate, non-overlapping directories.
They cannot use the same directory or contain one another's directories.

Before its first synchronization, a new Project has no source. Its Script Files
tab is still available because candidates come from the Data Source's file inventory.

## What a synchronization does

After a Data Source synchronizes, each Project reconciles its whole directory
in a background Job. This requires an RQ worker. A failure in one Project does not
affect the other Projects or the Data Source.

Reconciliation stages the complete directory, not just changed files. Deleted
files are absent from the next snapshot. Templates, data and other non-Python
files are included alongside source modules.

When the directory and Script File selection are unchanged, the existing revision
is reused. It may still be validated or activated, depending on its status and the
Project's activation settings.

Compiled Python files and `__pycache__` directories are skipped. Other violations
of the [source path policy](configuration.md#source-path-policy) produce an
`invalid` revision naming the affected paths. The active revision stays in service.

## A new file is a candidate, not a script file

A new Python file becomes a **candidate**. Select it and save the Script Files
tab to declare it as a file used for discovery. Synchronization does not change
your selection. Selected files can still import undeclared helpers, as described
in [Authoring](../user-guide/authoring.md#publishing-scripts-from-a-project).

When the source contains exactly one importable module, the tab selects it by
default. The default alone creates no declaration and publishes no Script. Save
the tab to confirm it. Clearing that selection and saving writes no declaration,
so the default appears again the next time you open the tab.

Each revision freezes the enabled Script File declarations. Saving a changed
selection applies it to the source the Project already holds, creating or reusing
a revision for that content and selection. Validation and any policy-driven
activation run in the background. The tab reports that work is in progress.

An unchanged selection stages and queues nothing.

Use **Reconcile Source** when you also need to stage the directory from the
Data Source's current inventory.

## Reconciling on demand

Choose **Reconcile Source** to rebuild a Project without waiting for another
synchronization. Use it after creating a Project between synchronizations, or when
you need its selection applied to the current source inventory.

Reconciliation does not synchronize the Data Source. To fetch newer files,
synchronize the Data Source from its own page first.

This action requires the Project's `reconcile` permission, separately from
`change`. See [Permissions](permissions.md).

## When a selected script file disappears

Deleting a selected Script File from the source makes the next revision invalid.
The revision's selection still names that path, so validation cannot import it
and reports the missing file.

The previous active revision stays in service. Check **Source state**, the
**Revisions** tab and the **Script Files** tab for the failure details.

If the file was removed intentionally, deselect it and reconcile again.

## Putting a revision in service

The activation policy applies after validation, just as it does for uploaded source.

| Policy | Behaviour |
|---|---|
| Automatic if valid | Reconciliation activates the revision after a `valid` verdict. |
| Manual | The revision remains `valid` until an operator chooses **Activate**. |

Activation verifies stored content again before changing the active revision.
The previous active revision is retired in the same step.

## Reverting the source

Returning to previous source with the same Script File selection reuses its
revision. If it already holds a successful validation result, it can be activated
without repeating validation. An automatic policy can put it back into service.
Under Manual policy, choose the eligible revision from the **Revisions** tab
and activate it.

Retired revisions keep their content, so you can return to a known-good version
without rebuilding it. Other reused revisions may still need processing, as
described in [What a synchronization does](#what-a-synchronization-does).

## What is not supported

| Area | Status |
|---|---|
| A manifest in the repository declaring its own script files | Script File selection is configured on the Project in NetBox. |
| Declared pip requirements | Revision requirements are not read or installed. |
| Reconciling one file at a time | Not planned. A revision holds the whole source tree. |
| Driving a Data Source's synchronization from a Project | Not planned. Synchronize the Data Source itself. |
