# Data Source Projects

A Script Project whose source type is `data_source` mirrors one directory of a NetBox
Core Data Source. The Data Source owns the files, and the Project decides what is published
from them. This page covers pointing a Project at a directory, what a synchronization does, and
why a Python file appearing in a repository publishes nothing on its own.

## Pointing a Project at a directory

*Custom Scripts > Script Projects*, then **Add**. Choose `Data source` as the source
type, which reveals the two fields it needs.

| Field | Meaning |
|---|---|
| Data source | The Core Data Source holding the files. |
| Data path | The directory within it whose contents are this Project's source. |
| Activation policy | Whether a revision that validates goes live by itself or waits for an operator. |

The data path is a directory relative to the Data Source root, with no leading slash.
Traversal segments are refused. It is compared segment by segment, so a Project at
`automation/netbox` does not claim `automation/netbox-old`. Leave it empty to root the
Project at the Data Source root, which takes every file in the source.

Two Projects on one Data Source cannot overlap. Neither may be the same directory as the
other, and neither may sit inside the other, because a file would then belong to two Python
package boundaries at once. A Project at the Data Source root contains every other path, so
a source with a root Project holds that Project alone.

Before the first synchronization the Project has no source and its page says so. The
Entrypoints tab is usable straight away, because the candidate list is read from the Data
Source's file inventory rather than from a revision.

## What a synchronization does

Each time the Data Source finishes synchronizing, every Project on it reconciles: the whole
directory as it stands becomes a new revision, which is validated and then activated if the
Project's policy allows. Reconciliation is a background Job, one per Project, so it needs a
running RQ worker, and one Project's failure leaves its siblings and the Data Source itself
untouched.

The complete directory is staged every time rather than a set of changes, so a file deleted
from the source is simply absent from the next revision. Every file is stored, not only Python
modules, because a script legitimately reads templates and data sitting next to it.

A synchronization that changed nothing produces nothing. A revision is addressed by the digest
of its content together with its entrypoint configuration, so an unchanged directory resolves
to the revision that already holds it and no second revision, validation, or activation
happens.

Compiled Python files and `__pycache__` directories are skipped wherever they sit, because
bytecode is not source and cannot be reviewed as source. Every other path the
[source path policy](configuration.md#source-path-policy) refuses is recorded instead: the new
revision is `invalid` and names the offending paths, and the Project keeps serving whatever it
served before.

## A new file is a candidate, not an entrypoint

A Python file that appears in the directory becomes a **candidate**. Nothing imports or
publishes it until somebody selects it on the Project's Entrypoints tab. That is what makes
synchronization safe to leave running: adding a file to a repository cannot publish a Custom
Script by itself, and the selection an administrator made is not overwritten by whatever the
repository happens to contain.

The selection is stored on the Project, and each revision freezes the enabled declarations at
the moment it is staged. Saving the Entrypoints tab therefore applies the change to the source
the Project already holds: the same content staged under the new entrypoint configuration is a
new revision, which is validated and activated like any other. That runs as a background job,
so the tab reports it is under way rather than showing the result.

Saving a selection that did not move stages nothing, because a revision is identified by its
content together with its entrypoint configuration, so the unchanged pair resolves to the
revision that already exists.

**Reconcile Source** does the same thing against the directory as it stands now, so use it
when the source has changed as well as the selection.

## Reconciling on demand

**Reconcile Source** on a Data Source-backed Project's page rebuilds its source from the
directory as it stands right now. It is the answer to two situations: a Project created between
synchronizations, which would otherwise have no source for hours, and an entrypoint selection
that should take effect without waiting.

It does not synchronize the Data Source itself. The file inventory NetBox already holds is what
a Project's source is built from, and refreshing that inventory is the Data Source's own
operation, on its own page.

The action needs the Project's `reconcile` permission, granted separately from `change`, because
what it changes is what the Project serves. See [Permissions](permissions.md).

## When a selected entrypoint disappears

Deleting a file that is a selected entrypoint is the one case where a synchronization produces
a revision that cannot work. The new revision holds the tree without that file while its
entrypoint snapshot still names it, so validation cannot import it and records an `invalid`
verdict naming the path.

That is the intended outcome, and the important part is what does not happen: the previous
revision keeps serving. Activation only ever follows a `valid` verdict, so a repository change
that breaks the source cannot take a working Project out of service. The Project's page reports
that its newest source failed validation, and the Revisions tab and the Entrypoints tab carry
the detail.

Deselect the entrypoint if the file is gone for good, then reconcile.

## Putting a revision in service

The Project's activation policy decides what happens after a `valid` verdict, exactly as it
does for uploaded source.

| Policy | Behaviour |
|---|---|
| Automatic if valid | Reconciliation activates the revision itself once the verdict is `valid`. |
| Manual | The revision stops at `valid` and waits for an operator to press **Activate**. |

Activation re-verifies the stored tree before moving the pointer, and retires the previous
revision in the same step.

## Reverting the source

Returning the directory to a state the Project has held before, by reverting a commit for
example, resolves to the revision that already holds that content. It carries a verdict
already, so there is nothing to validate, and an automatically activated Project puts that
revision back into service. A manually activated one offers it on the Revisions tab, where it
is one of the retired revisions that can be activated again.

This is why revisions are retired rather than deleted: going back to a known-good tree is a
matter of choosing it, not of rebuilding it.

## What is not supported yet

| Area | Status |
|---|---|
| A manifest in the repository declaring its own entrypoints | Planned. Entrypoint selection is a Project setting, made in NetBox |
| Declared pip requirements | Planned. A revision's requirements are not read or installed |
| Reconciling one file at a time | Not planned. A revision is a whole tree by design |
| Driving a Data Source's synchronization from a Project | Not planned. Synchronize the Data Source itself |
