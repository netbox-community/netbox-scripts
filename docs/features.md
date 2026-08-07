# Features

## Overview

NetBox Custom Scripts manages Custom Script source trees as first-class NetBox
objects. Administrators define projects that either own uploaded content or
mirror a directory of a NetBox Data Source, and get the standard NetBox object
experience: list and detail views, filtering, tags, custom fields, change
logging, global search, REST, and GraphQL.

## Core concepts

| Concept | Definition | More detail |
|---|---|---|
| Custom Script Project | One complete script source tree, the ownership and Python package boundary. | [Custom Script Project](models/customscriptproject.md) |
| Key | Stable user-facing project identifier (slug), immutable after creation. | [Custom Script Project](models/customscriptproject.md) |
| Storage key | Immutable internal storage and runtime identity (UUID), not a public identifier. | [Custom Script Project](models/customscriptproject.md) |
| Source type | Where project content comes from: `upload` or `data_source`, never mixed. | [Custom Script Project](models/customscriptproject.md) |
| Activation policy | Whether future synchronized revisions activate manually or automatically when valid. | [Custom Script Project](models/customscriptproject.md) |
| Project revision | One immutable snapshot of a project's source tree and entrypoint configuration, addressed by a content digest and an entrypoint digest. | [Custom Script Project Revision](models/customscriptprojectrevision.md) |
| Active revision | The single revision of a project that is currently active. Activating another retires it. | [Custom Script Project Revision](models/customscriptprojectrevision.md) |
| Custom Script Module | One declared entrypoint file that discovery imports and publishes Custom Scripts from. | [Custom Script Module](models/customscriptmodule.md) |
| Entrypoint snapshot | The enabled module declarations frozen into a revision at staging time. | [Custom Script Project Revision](models/customscriptprojectrevision.md) |
| Custom Script | One published Script class, derived from an activated revision and retired rather than deleted. | [Custom Script](models/customscript.md) |
| Run | One execution of a Custom Script, recorded as a Job and pinned to the revision that was being served when it was requested. | [Running Custom Scripts](execution.md) |
| Commit and dry run | Whether a run's database changes are kept or rolled back when it finishes. | [Running Custom Scripts](execution.md) |
| Revision validation | The leased background step that imports a revision's entrypoints and records a `valid` or `invalid` verdict. | [Runtime and Loading](runtime.md) |
| Source state | The plain-language summary of whether a Project is serving its newest source, and what it is waiting on if not. | [Uploading Scripts](uploading.md) |
| Reconciliation | Rebuilding a Data Source-backed project's source from the whole directory as it stands, after a synchronization or on demand. | [Data Source Projects](data-sources.md) |
| Entrypoint candidate | A Python file in a project's source that could be declared an entrypoint, and publishes nothing until it is. | [Data Source Projects](data-sources.md) |
| Private runtime namespace | The generated package names revision code imports under, isolating projects, revisions, and installed packages from each other. | [Runtime and Loading](runtime.md) |

## Supported workflows

| Workflow | User | Outcome |
|---|---|---|
| Manage projects | Administrator | Create, edit, delete, bulk-import, and tag Custom Script Projects. |
| Upload a script | Administrator | Create a Project from one `.py` file, or add another to an existing one, without naming a path. See [Uploading Scripts](uploading.md). |
| Activate a revision | Operator | Put a validated revision into service, automatically on a valid verdict or by hand for a manually activated Project. |
| Follow a Project's source | Operator | Read the current revision and a plain-language source state on the Project's page, and its full history on the Revisions tab. |
| Query revision history | Operator | Read a Project's revisions and their validation outcomes over REST or GraphQL, filtered by project, status, or digest. |
| Select entrypoints | Administrator | Choose which of a Project's source modules discovery imports, on its Entrypoints tab or over REST, without typing a path. |
| Configure a Data Source-backed project | Administrator | Point a project at a Core Data Source and a directory within it. See [Data Source Projects](data-sources.md). |
| Track a Data Source directory | Operator | Every synchronization of the Data Source rebuilds the project's source from the whole directory, validates it, and activates it when the policy allows. |
| Reconcile a project on demand | Administrator | Rebuild a Data Source-backed project's source from the current file inventory without waiting for the next synchronization. |
| Browse published scripts | Operator | List, search, and filter every published Custom Script, or read a Project's own on its detail page. |
| Enable or disable a script | Administrator | Toggle `enabled` on a published Custom Script, one at a time or in bulk, without affecting what synchronization owns. |
| Run a script | Operator | Fill in the form the script declares and queue a run, committing its changes or reverting them as a dry run. See [Running Custom Scripts](execution.md). |
| Read a run | Operator | Follow one run's status, log and output on its result page, and every run a script has performed on its Jobs tab. |
| Query projects | Automation | Filter projects via REST and GraphQL, including typed choice enums in GraphQL filters. |
| Author Custom Scripts | Developer | Write scripts against the plugin's [authoring API](authoring.md): Script base classes, variables, logging, and form generation. |
| Publish scripts from a project | Developer | Declare entrypoint modules and control what a revision offers through the [discovery rules](authoring.md#publishing-scripts-from-a-project). |
| Validate revisions | Operator | Run the validation job to drive a materialized revision to a `valid` or `invalid` verdict with sanitized, inspectable errors. |

## Not yet implemented

The following areas are intentionally absent from this release and arrive in
follow-up releases:

| Area | Status |
|---|---|
| Uploading helper modules, archives, and other resources | Planned, uploads are one executable module at a time. A Project needing helpers is managed through a Data Source |
| A repository manifest declaring its own entrypoints | Planned, entrypoint selection is a Project setting made in NetBox |
| Requesting a run over REST | Planned, runs are requested from the UI |
| Event Rule actions | Planned |
| Migration from NetBox's built-in Custom Scripts | Planned |
