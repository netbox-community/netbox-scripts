# NetBox Scripts

Custom Scripts for NetBox

## Overview

NetBox Scripts brings project-based management of Custom Scripts to
NetBox. A Script Project represents one complete, internally consistent
script source tree. It is the ownership boundary for source files and the
Python package boundary used when loading and executing scripts. Projects own
either uploaded content or a directory of a NetBox Data Source, never both.

The current release is alpha. It ships the project and script file models with
full UI, REST API, GraphQL, and global-search surfaces, the plugin-owned script
authoring API with variable classes, form generation, and structured logging,
immutable project revisions held in plugin-owned project storage with manifest
verification, activation, and cleanup, and the loading engine: declared
script files, a manifest-verified runtime cache, a name-isolating package
loader, Script discovery, and leased revision validation. Source reaches
a Project two ways, both driven through staging, validation, discovery, and
activation: one uploaded script at a time, or a directory of a Data Source
rebuilt every time that source synchronizes. Published `NetBoxScript` objects and
execution complete the path, so a script can be run against the revision its
Project is serving. See [Features](features.md) for the capability breakdown and
current boundaries.

## Documentation

| Page | What it covers |
|---|---|
| [Features](features.md) | What NetBox Scripts adds to NetBox |
| [Quickstart](quickstart.md) | Install and enable the plugin |
| [Configuration](configuration.md) | Supported settings |
| [Uploading Scripts](uploading.md) | Adding source to a Project and putting it in service |
| [Data Source Projects](data-sources.md) | Mirroring a Data Source directory, and what a synchronization does |
| [Migration](migration.md) | Moving off NetBox's built-in Custom Scripts |
| [Authoring](authoring.md) | Writing Scripts against the plugin API |
| [Runtime and Loading](runtime.md) | How revisions load, get discovered, and validate |
| [Running Scripts](execution.md) | Requesting a run, revision pinning, commit and dry run, reading a result |
| [Event Rules](event-rules.md) | Running a Script from a rule, and reacting to the plugin's own objects |
| [Permissions](permissions.md) | The standard permissions and the four the plugin adds |
| [Releases](releases.md) | Version history and upgrade notes |

## Compatibility

| Runtime | Supported range |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12+ |
