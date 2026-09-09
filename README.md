# NetBox Scripts

Custom Scripts for NetBox, maintained by [NetBox Labs](https://netboxlabs.com/).

The plugin provides project-based management of NetBox Custom Scripts: each
Script Project is the ownership boundary for one script source tree and
the Python package boundary used when loading and executing scripts.

## Status

This is an alpha release. The complete path from source to a running script
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
- script file selection as a Project setting, on a tab or over REST
- the loading engine: a manifest-verified runtime cache, a private package
  loader, Script discovery, and leased revision validation
- execution: running a script from the UI or over REST, committing or as a dry
  run, deferred and recurring runs, and a result page with the run log
- running a Script from an Event Rule, and using the plugin's own objects
  as Event Rule sources (the action needs NetBox 4.7)
- migration off the built-in Custom Scripts feature: an inventory pass, a
  staging pass, and a cutover that moves Job history, Event Rules, permissions,
  and schedules onto the plugin
- per-installation overrides for a script's timeout, notification policy, and
  commit default, with an unset override following the script class

Not yet implemented, planned for follow-up releases:

- uploading helper modules, archives, and other resources, since an upload is
  one script file at a time. A Project needing helpers uses a Data Source
- a manifest in the repository declaring its own script files
- declared pip requirements, which are neither read nor installed
- recording the input values a run was given

## Compatibility

| Plugin Version | Minimum NetBox | Maximum NetBox | Minimum Python |
|----------------|----------------|----------------|----------------|
| 0.0.1 | 4.7.0 | 4.7.99 | 3.12 |

The full per-release matrix lives in [COMPATIBILITY.md](https://github.com/netbox-community/netbox-scripts/blob/main/COMPATIBILITY.md).

## Installation

Once the release is published on PyPI, install it into the NetBox virtualenv:

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

Everything in `PLUGINS_CONFIG` is optional and has a working default. Project storage
is configured separately and is not optional: source is written to the backend
registered under the `netbox_scripts` key of NetBox's `STORAGES` setting. On a single
node, Django's own `FileSystemStorage` is enough:

```python
STORAGES = {
    'netbox_scripts': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {
            'location': '/var/lib/netbox-scripts',
        },
    },
}
```

NetBox merges this with its built-in entries, so defining only this key leaves
`default` and the others intact. See the [configuration guide](https://github.com/netbox-community/netbox-scripts/blob/main/docs/configuration.md)
for object storage, for the `netbox_scripts.W001` check that reports a missing entry,
and for why it does not fall back to NetBox's `default` storage.

Apply database migrations:

```bash
python manage.py migrate
```

## Documentation

User documentation lives under [`docs/`](https://github.com/netbox-community/netbox-scripts/tree/main/docs).

## Support

File issues in this repository.

## License

Apache License 2.0, see [LICENSE](https://github.com/netbox-community/netbox-scripts/blob/main/LICENSE).
Copyright (c) 2026 NetBox Labs.
