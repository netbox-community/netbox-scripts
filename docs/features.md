# Features

## Overview

NetBox Scripts manages Python scripts and their source in NetBox. Create Projects
from uploads or Data Source directories, then validate and activate their source.

The plugin integrates with NetBox's lists, filtering, tags, custom fields, change
logging, search, REST and GraphQL. The model references describe the interfaces
available for each object.

## Core concepts

| Concept | Definition | More detail |
|---|---|---|
| Script Project | A complete source tree and its settings, forming one Python package boundary. | [Script Project](reference/models/scriptproject.md) |
| Key | A stable user-facing Project slug, fixed after creation. | [Script Project](reference/models/scriptproject.md) |
| Storage key | An internal UUID identifying storage and runtime content, fixed after creation. | [Script Project](reference/models/scriptproject.md) |
| Source type | Either `upload` or `data_source`. A Project cannot mix them. | [Script Project](reference/models/scriptproject.md) |
| Activation policy | Whether valid synchronized revisions activate automatically or manually. | [Script Project](reference/models/scriptproject.md) |
| Project revision | A source snapshot and Script File configuration, identified by both digests. | [Script Project Revision](reference/models/scriptprojectrevision.md) |
| Active revision | The revision currently in service. Activating another retires it. | [Script Project Revision](reference/models/scriptprojectrevision.md) |
| Script File | A declared file imported for Script discovery and publication. | [Script File](reference/models/scriptfile.md) |
| Script file snapshot | The enabled declarations captured when staging a revision. | [Script Project Revision](reference/models/scriptprojectrevision.md) |
| Script | A published class from an activated revision. It is retired rather than deleted when no longer published. | [Script](reference/models/netboxscript.md) |
| Run | A Script execution recorded as a Job. One-shot runs pin the revision active when they are requested. Recurring occurrences use the active revision when they start. | [Running Scripts](user-guide/execution.md) |
| Commit and dry run | Whether database changes are kept or rolled back. | [Running Scripts](user-guide/execution.md) |
| Revision validation | A background operation with a time-limited claim that imports selected files and records a `valid` or `invalid` result. | [Runtime and Loading](reference/runtime.md) |
| Source state | A summary of whether the Project serves its newest source and what it is waiting for. | [Uploading Scripts](administration/uploading.md) |
| Reconciliation | Rebuilding a Data Source Project from its directory, after synchronization or on demand. | [Data Source Projects](administration/data-sources.md) |
| Script file candidate | A Python file available for selection as a Script File. | [Data Source Projects](administration/data-sources.md) |
| Private runtime namespace | Separate import names prevent naming conflicts. This is not a sandbox. Code runs with the permissions of the process loading it. | [Runtime and Loading](reference/runtime.md) |

## Supported workflows

| Workflow | User | Outcome |
|---|---|---|
| Manage projects | Administrator | Create, edit, delete, bulk-import and tag Projects. |
| Upload a script | Administrator | Create a Project from one `.py` file or add a file to an upload-based Project. See [Uploading Scripts](administration/uploading.md). |
| Activate a revision | Operator | Put a validated revision into service automatically under its policy or through manual activation. |
| Follow a Project's source | Operator | Check Source state, the current revision and the Revisions tab. |
| Repair published scripts | Operator | Restore Script rows from the serving revision and see how many changed. |
| List a Project's files | Operator | Read sizes, checksums and Script File status on Revision Files. Missing declared paths are marked as removed or awaiting newer source. |
| Query revision history | Operator | Filter revision history and validation results through REST or GraphQL by Project, status or digest. |
| Select script files | Administrator | Choose which files discovery imports, from the Script Files tab or over REST. |
| Configure a Data Source-backed project | Administrator | Select a Data Source directory. See [Data Source Projects](administration/data-sources.md). |
| Track a Data Source directory | Operator | Reconcile after synchronization, with validation and activation according to revision state and Project policy. |
| Reconcile a project on demand | Administrator | Rebuild from the current Data Source inventory without waiting for synchronization. |
| Migrate off the built-in feature | Administrator | Inventory existing Custom Scripts, stage their source without activation, then cut over. See [Migration](administration/migration.md). |
| Browse published scripts | Operator | List, search and filter Scripts, or browse a Project's Scripts. |
| Enable or disable a script | Administrator | Change `enabled` individually or in bulk without changing source-derived metadata. |
| Run a script | Operator | Complete its form and queue a committed run or dry run. See [Running Scripts](user-guide/execution.md). |
| Read a run | Operator | Check status, logs and output on the result page, and available history on the Jobs tab. |
| Run a script from an Event Rule | Administrator | Trigger a run using a rule's action data. See [Event Rules](administration/event-rules.md). |
| React to object changes | Administrator | Use Event Rules or webhooks for supported changes. See [Event Rules](administration/event-rules.md#scripts-as-event-sources) for delivery conditions. |
| Query projects | Automation | Filter through REST and GraphQL, including GraphQL choice enums. |
| Author Scripts | Script author | Use Script classes, variables, logging and form generation. See [Authoring](user-guide/authoring.md). |
| Publish scripts from a project | Script author | Select Script Files and control publication through the [discovery rules](user-guide/authoring.md#publishing-scripts-from-a-project). |
| Validate revisions | Operator | Validate a materialized revision and inspect its `valid` or `invalid` result and sanitized errors. |

## Execution defaults

Administrators can override a Script's timeout, notification policy and commit
default through the UI and REST. Unset overrides follow class defaults. Each
recurring successor resolves its timeout from the current Script configuration.

## What is not in this release

| Area | What that means |
|---|---|
| Uploading helper modules, archives, and other resources | Uploads accept one Script File at a time. Use a Data Source for helper files and resources. |
| A repository manifest declaring its own script files | Select Script Files on the Project in NetBox. |
| Declared pip requirements | Revision dependencies are not read or installed. |
| Recorded input values | Jobs record the Script, revision and result, but not submitted inputs. |
| Recurring runs carrying an upload | An uploaded file can be used for a one-shot run, not a recurrence. |
| Legacy Reports | Classes with `test_*` methods and no `run()` are rejected. Rewrite them as Scripts with a `run()` method. |
| A permanent `extras.scripts` layer | Legacy imports are transitional and are expected to stop working with NetBox v5.0. Move to the plugin's authoring API before that upgrade. |
