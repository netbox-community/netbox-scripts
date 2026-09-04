# NetBox Scripts

Custom Scripts for NetBox, maintained by [NetBox Labs](https://netboxlabs.com/).

The plugin provides project-based management of NetBox Custom Scripts: each
Script Project is the ownership boundary for one script source tree and
the Python package boundary used when loading and executing scripts.

## Status

This is a pre-alpha release. The complete path from source to a running script
works end to end.

Implemented:

- four models with full UI, REST API, GraphQL, and global-search surfaces:
  `ScriptProject`, `ScriptProjectRevision`, `ScriptFile`,
  and `NetBoxScript`
- the plugin-owned script authoring API: `Script` base classes, variable
  types, dynamic form generation, structured logging, and `AbortScript`
- compatibility with the built-in authoring API, so a script importing from
  `extras.scripts` runs unchanged
- immutable project revisions, each addressed by a canonical manifest and a
  content digest, held in plugin-owned storage with verified reads, activation,
  and cleanup on deletion
- both source routes: uploading one script at a time, and mirroring a directory
  of a NetBox Data Source that rebuilds on every synchronization
- entrypoint selection as a Project setting, on a tab or over REST
- the loading engine: a manifest-verified runtime cache, a private package
  loader, Custom Script discovery, and leased revision validation
- execution: running a script from the UI or over REST, committing or as a dry
  run, deferred and recurring runs, and a result page with the run log
- running a Custom Script from an Event Rule, and using the plugin's own objects
  as Event Rule sources (the action needs NetBox 4.7)
- migration off the built-in Custom Scripts feature: an inventory pass, a
  staging pass, and a cutover that moves Job history, Event Rules, permissions,
  and schedules onto the plugin

Not yet implemented, planned for follow-up releases:

- uploading helper modules, archives, and other resources, since an upload is
  one executable module at a time. A Project needing helpers uses a Data Source
- a manifest in the repository declaring its own entrypoints
- declared pip requirements, which are neither read nor installed
- recording the input values a run was given
- overriding the timeout, notification policy, and commit default per
  installation

## Compatibility

| Plugin Version | Minimum NetBox | Maximum NetBox | Minimum Python |
|----------------|----------------|----------------|----------------|
| 0.0.1 | 4.7.0 | 4.7.99 | 3.12 |

The full per-release matrix lives in [COMPATIBILITY.md](COMPATIBILITY.md).

## Installation

Install into the NetBox virtualenv from the NetBox Labs artifact source:

```bash
pip install netbox-scripts
```

For local development, run `pip install -e .` from the repo root.

Enable the plugin in NetBox's `configuration.py`:

```python
PLUGINS = [
    'netbox_scripts',
]
```

The plugin reads optional storage settings from `PLUGINS_CONFIG`, none of which are
required to enable it. See the configuration documentation for the full list.

Apply database migrations:

```bash
python manage.py migrate
```

## Documentation

User documentation lives under [`docs/`](docs/) and is published at
<https://netboxlabs.github.io/netbox-custom-scripts/>.

## Support

File issues in this repository.

## License

NetBox Limited Use License 1.0, see [LICENSE.md](LICENSE.md).
Copyright (c) 2026 NetBox Labs.
