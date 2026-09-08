# Features

## Overview

NetBox Scripts manages Script source trees as first-class NetBox
objects. Administrators define projects that either own uploaded content or
mirror a directory of a NetBox Data Source, and get the standard NetBox object
experience: list and detail views, filtering, tags, custom fields, change
logging, global search, REST, and GraphQL.

## Core concepts

| Concept | Definition | More detail |
|---|---|---|
| Script Project | One complete script source tree, the ownership and Python package boundary. | [Script Project](models/scriptproject.md) |
| Key | Stable user-facing project identifier (slug), immutable after creation. | [Script Project](models/scriptproject.md) |
| Storage key | Immutable internal storage and runtime identity (UUID), not a public identifier. | [Script Project](models/scriptproject.md) |
| Source type | Where project content comes from: `upload` or `data_source`, never mixed. | [Script Project](models/scriptproject.md) |
| Activation policy | Whether future synchronized revisions activate manually or automatically when valid. | [Script Project](models/scriptproject.md) |
| Project revision | One immutable snapshot of a project's source tree and script file configuration, addressed by a content digest and a script file digest. | [Script Project Revision](models/scriptprojectrevision.md) |
| Active revision | The single revision of a project that is currently active. Activating another retires it. | [Script Project Revision](models/scriptprojectrevision.md) |
| Script File | One declared file that discovery imports and publishes Scripts from. | [Script File](models/scriptfile.md) |
| Script file snapshot | The enabled script file declarations frozen into a revision at staging time. | [Script Project Revision](models/scriptprojectrevision.md) |
| Script | One published Script class, derived from an activated revision and retired rather than deleted. | [Script](models/netboxscript.md) |
| Run | One execution of a Script, recorded as a Job and pinned to the revision that was being served when it was requested. | [Running Scripts](execution.md) |
| Commit and dry run | Whether a run's database changes are kept or rolled back when it finishes. | [Running Scripts](execution.md) |
| Revision validation | The leased background step that imports a revision's script files and records a `valid` or `invalid` verdict. | [Runtime and Loading](runtime.md) |
| Source state | The plain-language summary of whether a Project is serving its newest source, and what it is waiting on if not. | [Uploading Scripts](uploading.md) |
| Reconciliation | Rebuilding a Data Source-backed project's source from the whole directory as it stands, after a synchronization or on demand. | [Data Source Projects](data-sources.md) |
| Script file candidate | A Python file in a project's source that could be declared a script file, and publishes nothing until it is. | [Data Source Projects](data-sources.md) |
| Private runtime namespace | The generated package names revision code imports under, keeping projects, revisions, and installed packages from colliding by **name**. It is not a sandbox: a revision runs with the worker's permissions. | [Runtime and Loading](runtime.md) |

## Supported workflows

| Workflow | User | Outcome |
|---|---|---|
| Manage projects | Administrator | Create, edit, delete, bulk-import, and tag Script Projects. |
| Upload a script | Administrator | Create a Project from one `.py` file, or add another to an existing one, without naming a path. See [Uploading Scripts](uploading.md). |
| Activate a revision | Operator | Put a validated revision into service, automatically on a valid verdict or by hand for a manually activated Project. |
| Follow a Project's source | Operator | Read the current revision and a plain-language source state on the Project's page, and its full history on the Revisions tab. |
| Repair published scripts | Operator | Republish a serving Project's Scripts from the revision it already serves, for rows that drifted from the snapshot they derive from. The Project reports how many rows moved, or that none did. |
| List a Project's files | Operator | Read the current revision's files with size, checksum, and script file state on the Revision Files tab. A declared path the served revision does not hold is annotated, and the annotation says which case it is: gone from the source, or waiting on a newer revision to be activated. |
| Query revision history | Operator | Read a Project's revisions and their validation outcomes over REST or GraphQL, filtered by project, status, or digest. |
| Select script files | Administrator | Choose which of a Project's source modules discovery imports, on its Script Files tab or over REST, without typing a path. |
| Configure a Data Source-backed project | Administrator | Point a project at a Core Data Source and a directory within it. See [Data Source Projects](data-sources.md). |
| Track a Data Source directory | Operator | Every synchronization of the Data Source rebuilds the project's source from the whole directory, validates it, and activates it when the policy allows. |
| Reconcile a project on demand | Administrator | Rebuild a Data Source-backed project's source from the current file inventory without waiting for the next synchronization. |
| Migrate off the built-in feature | Administrator | Report what moving off NetBox's built-in Custom Scripts would do, stage that content as inactive Projects, then cut over and move Job history, Event Rules, permissions, and schedules onto the plugin. See [Migration](migration.md). |
| Browse published scripts | Operator | List, search, and filter every published Script, or read a Project's own on its detail page. |
| Enable or disable a script | Administrator | Toggle `enabled` on a published Script, one at a time or in bulk, without affecting what synchronization owns. |
| Run a script | Operator | Fill in the form the script declares and queue a run, committing its changes or reverting them as a dry run. See [Running Scripts](execution.md). |
| Read a run | Operator | Follow one run's status, log and output on its result page, and every run a script has performed on its Jobs tab. |
| Run a script from an Event Rule | Administrator | Have a rule run a Script when something happens, passing its own data as the script's input. See [Event Rules](event-rules.md). |
| React to a Script object | Administrator | Point an Event Rule or webhook at any of the plugin's own object types, so an invalid revision or a retired script can raise an alert. See [Event Rules](event-rules.md). |
| Query projects | Automation | Filter projects via REST and GraphQL, including typed choice enums in GraphQL filters. |
| Author Scripts | Developer | Write scripts against the plugin's [authoring API](authoring.md): Script base classes, variables, logging, and form generation. |
| Publish scripts from a project | Developer | Declare script files and control what a revision offers through the [discovery rules](authoring.md#publishing-scripts-from-a-project). |
| Validate revisions | Operator | Run the validation job to drive a materialized revision to a `valid` or `invalid` verdict with sanitized, inspectable errors. |

## Execution defaults

Each Script supports administrator overrides for its timeout, notification policy,
and commit default through the UI and REST. Unset values follow the class defaults.
Recurring successors resolve the timeout from the current Script configuration.

## Not yet implemented

The following areas are intentionally absent from this release and arrive in
follow-up releases:

| Area | Status |
|---|---|
| Uploading helper modules, archives, and other resources | Planned, uploads are one script file at a time. A Project needing helpers is managed through a Data Source |
| A repository manifest declaring its own script files | Planned, script file selection is a Project setting made in NetBox |
| Declared pip requirements | Planned, a revision's external dependencies are neither read nor installed |
| Recorded input values | Planned, a run records which script and revision ran and the result, but not the values submitted |
