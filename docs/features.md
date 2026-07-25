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
| Project revision | One immutable snapshot of a project's complete source tree, addressed by a content digest. | [Custom Script Project Revision](models/customscriptprojectrevision.md) |
| Active revision | The single revision of a project that is currently active. Activating another retires it. | [Custom Script Project Revision](models/customscriptprojectrevision.md) |

## Supported workflows

| Workflow | User | Outcome |
|---|---|---|
| Manage projects | Administrator | Create, edit, delete, bulk-import, and tag Custom Script Projects. |
| Configure a Data Source-backed project | Administrator | Point a project at a Core Data Source and a directory within it. |
| Query projects | Automation | Filter projects via REST and GraphQL, including typed choice enums in GraphQL filters. |
| Author Custom Scripts | Developer | Write scripts against the plugin's [authoring API](authoring.md): Script base classes, variables, logging, and form generation. |

## Not yet implemented

The following areas are intentionally absent from this release and arrive in
follow-up releases:

| Area | Status |
|---|---|
| Project modules | Planned |
| Script discovery | Planned |
| Running authored scripts | Planned |
| Source uploads | Planned |
| Revision UI and API surfaces | Planned |
| Data Source synchronization | Planned |
| Package loading, execution, and scheduling | Planned |
| Event Rule actions | Planned |
| Migration from NetBox's built-in Custom Scripts | Planned |
