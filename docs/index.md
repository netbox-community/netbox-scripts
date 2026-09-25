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

| What you need to do | Start here |
|---|---|
| Run or schedule a Script | [Running scripts](user-guide/execution.md) |
| Write or adapt a Script | [Writing scripts](user-guide/authoring.md) |
| Install or administer the plugin | [Installation](administration/installation.md), [Configuration](administration/configuration.md) and [Permissions](administration/permissions.md) |
| Contribute to the plugin | [Contributing](development/contributing.md) |

**Already using NetBox's built-in Custom Scripts?** Read the
[migration guide](administration/migration.md) before moving your scripts.

Legacy Reports are not supported. Imports from `extras.scripts` are a
transitional option and stop working at NetBox v5.0. See
[Authoring](user-guide/authoring.md) for supported imports and the changes to
make before upgrading.

## Main concepts

| Term | Meaning |
|---|---|
| Project | Source files and settings for a group of scripts, using either uploads or a Data Source directory. |
| Script File | A Python file in a Project selected for discovering runnable Scripts. |
| Revision | An immutable snapshot of a Project's source and its selected Script Files. |
| Script | A runnable Python class published from an activated revision. |

A new revision does not replace the active one until activation succeeds.

## Compatibility

| Runtime | Supported range |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12+ |
