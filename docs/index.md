# NetBox Custom Scripts

Custom Scripts for NetBox

## Overview

NetBox Custom Scripts brings project-based management of Custom Scripts to
NetBox. A Custom Script Project represents one complete, internally consistent
script source tree. It is the ownership boundary for source files and the
Python package boundary used when loading and executing scripts. Projects own
either uploaded content or a directory of a NetBox Data Source, never both.

The current release is pre-alpha. It ships the project and module models with
full UI, REST API, GraphQL, and global-search surfaces, the plugin-owned script
authoring API with variable classes, form generation, and structured logging,
immutable project revisions held in plugin-owned project storage with manifest
verification, activation, and cleanup, and the loading engine: declared
entrypoint modules, a manifest-verified runtime cache, an isolating package
loader, Custom Script discovery, and leased revision validation. Source
uploads, Data Source synchronization, the revision object surface, and
execution are planned follow-ups. See [Features](features.md) for the
capability breakdown and current boundaries.

## Documentation

| Page | What it covers |
|---|---|
| [Features](features.md) | What NetBox Custom Scripts adds to NetBox |
| [Quickstart](quickstart.md) | Install and enable the plugin |
| [Configuration](configuration.md) | Supported settings |
| [Authoring](authoring.md) | Writing Custom Scripts against the plugin API |
| [Runtime and Loading](runtime.md) | How revisions load, get discovered, and validate |
| [Releases](releases.md) | Version history and upgrade notes |

## Compatibility

| Runtime | Supported range |
|---|---|
| NetBox | 4.6.0 to 4.7.99 |
| Python | 3.12+ |
