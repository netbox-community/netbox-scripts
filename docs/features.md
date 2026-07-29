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
| Revision validation | The leased background step that imports a revision's entrypoints and records a `valid` or `invalid` verdict. | [Runtime and Loading](runtime.md) |
| Private runtime namespace | The generated package names revision code imports under, isolating projects, revisions, and installed packages from each other. | [Runtime and Loading](runtime.md) |

## Supported workflows

| Workflow | User | Outcome |
|---|---|---|
| Manage projects | Administrator | Create, edit, delete, bulk-import, and tag Custom Script Projects. |
| Configure a Data Source-backed project | Administrator | Point a project at a Core Data Source and a directory within it. |
| Query projects | Automation | Filter projects via REST and GraphQL, including typed choice enums in GraphQL filters. |
| Author Custom Scripts | Developer | Write scripts against the plugin's [authoring API](authoring.md): Script base classes, variables, logging, and form generation. |
| Publish scripts from a project | Developer | Declare entrypoint modules and control what a revision offers through the [discovery rules](authoring.md#publishing-scripts-from-a-project). |
| Validate revisions | Operator | Run the validation job to drive a materialized revision to a `valid` or `invalid` verdict with sanitized, inspectable errors. |

## Not yet implemented

The following areas are intentionally absent from this release and arrive in
follow-up releases:

| Area | Status |
|---|---|
| Running authored scripts | Planned |
| Source uploads | Planned |
| Module, revision, and script UI and API surfaces | Planned |
| Data Source synchronization | Planned |
| Automatic validation and activation triggers | Planned |
| Execution and scheduling | Planned |
| Event Rule actions | Planned |
| Migration from NetBox's built-in Custom Scripts | Planned |
