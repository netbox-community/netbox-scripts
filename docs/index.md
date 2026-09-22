# NetBox Scripts

Manage, validate and run Python scripts in NetBox.

Upload scripts or connect a Project to a directory in a NetBox Data Source.
The plugin keeps source in immutable revisions, validates changes before
activation and lets each Project serve one active revision at a time.

!!! warning "Alpha release"

    **NetBox Scripts is in alpha and is not recommended for production use.**
    Expect bugs and changes to features, APIs and upgrade procedures before a
    stable release. Please try it in a test environment and back up your data
    before upgrading or migrating from NetBox's built-in Custom Scripts.

## Getting started

Start with the [Quickstart](quickstart.md) to install and configure the plugin.
Then [upload your scripts](uploading.md) or
[connect a Data Source directory](data-sources.md).

Already using NetBox's built-in Custom Scripts? Read the
[migration guide](migration.md) before moving your scripts. Two boundaries
to know before you start: legacy Reports are refused rather than emulated,
and `extras.scripts` imports work as a transitional layer that stops working
at NetBox v5.0. Both are covered in [Authoring](authoring.md).

## Main concepts

| Term | Meaning |
|---|---|
| Project | Source files and settings for a group of scripts, using either uploads or a Data Source directory. |
| Script File | A Python file in a Project selected for discovering runnable Scripts. |
| Revision | An immutable snapshot of a Project's source and its selected Script Files. |
| Script | A runnable Python class published from an activated revision. |

A new revision does not replace the active one until activation succeeds.

## Documentation

| Guide | What it covers |
|---|---|
| [Features](features.md) | Supported functionality and current limitations. |
| [Quickstart](quickstart.md) | Install and enable the plugin. |
| [Configuration](configuration.md) | Configure source storage and plugin settings. |
| [Uploading Scripts](uploading.md) | Add files to a Project and activate your scripts. |
| [Data Source Projects](data-sources.md) | Connect a Data Source directory and understand synchronization. |
| [Migration](migration.md) | Move from NetBox's built-in Custom Scripts. |
| [Authoring](authoring.md) | Write scripts using the plugin's authoring API. |
| [Runtime and Loading](runtime.md) | Understand how Scripts are discovered, validated and loaded. |
| [Running Scripts](execution.md) | Run and schedule scripts, use dry runs and read results. |
| [Event Rules](event-rules.md) | Run scripts from events and use plugin objects in Event Rules. |
| [Permissions](permissions.md) | Manage access with NetBox's standard and plugin-specific permissions. |
| [Releases](releases.md) | Version history and upgrade notes. |

## Compatibility

| Runtime | Supported range |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12+ |
