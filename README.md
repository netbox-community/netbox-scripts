# NetBox Custom Scripts

Custom Scripts for NetBox, maintained by [NetBox Labs](https://netboxlabs.com/).

The plugin provides project-based management of NetBox Custom Scripts: each
Custom Script Project is the ownership boundary for one script source tree and
the Python package boundary used when loading and executing scripts.

## Status

This is a pre-alpha release.

Implemented:

- the `CustomScriptProject` model with full UI, REST API, GraphQL, and
  global-search surfaces
- the plugin-owned script authoring API: `Script` base classes, variable
  types, dynamic form generation, structured logging, and `AbortScript`
- immutable project revisions, each addressed by a canonical manifest and a
  content digest
- plugin-owned project storage: revision staging, filesystem verification,
  activation, and cleanup on deletion

Not yet implemented, planned for follow-up releases:

- source uploads and Data Source synchronization
- project modules and entrypoint selection
- script discovery and semantic validation
- package loading
- execution and scheduling
- Event Rule actions
- revision UI and API surfaces
- migration from NetBox's built-in Custom Scripts

## Compatibility

| Plugin Version | Minimum NetBox | Maximum NetBox | Minimum Python |
|----------------|----------------|----------------|----------------|
| 0.0.1 | 4.6.0 | 4.7.99 | 3.12 |

The full per-release matrix lives in [COMPATIBILITY.md](COMPATIBILITY.md).

## Installation

Install into the NetBox virtualenv from the NetBox Labs artifact source:

```bash
pip install netbox-custom-scripts
```

For local development, run `pip install -e .` from the repo root.

Enable the plugin in NetBox's `configuration.py`:

```python
PLUGINS = [
    'netbox_custom_scripts',
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
