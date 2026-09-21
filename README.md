# NetBox Scripts

Manage, validate and run Python scripts in NetBox.

Upload scripts or connect a Project to a directory in a NetBox Data Source.
The plugin keeps source in immutable revisions, validates changes before
activation and lets each Project serve one active revision at a time.

> [!WARNING]
> **NetBox Scripts is in alpha and is not recommended for production use.**
> Expect bugs and changes to features, APIs and upgrade procedures before a
> stable release. Please try it in a test environment and back up your data
> before upgrading or migrating from NetBox's built-in Custom Scripts.

## What you can do

- **Run scripts where you need them.** Use the NetBox interface, REST API,
  Event Rules or command line, with support for dry runs, scheduled runs and
  recurring execution.
- **Set defaults for each script.** Configure its timeout, notification policy
  and commit behavior.
- **Use familiar NetBox interfaces.** Browse Projects, Script Files and Scripts
  through the UI, REST API, GraphQL and global search, with read-only views of
  revision details.
- **Bring your existing scripts.** Keep using `extras.scripts` imports or adopt
  the plugin's authoring API. A migration workflow helps you move from NetBox's
  built-in Custom Scripts.

See the [Features guide](https://netbox-community.github.io/netbox-scripts/features/)
for supported functionality and current limitations.

## Compatibility

| Plugin version | Minimum NetBox | Maximum NetBox | Minimum Python |
|----------------|----------------|----------------|----------------|
| 0.0.1 | 4.7.0 | 4.7.99 | 3.12 |

See [COMPATIBILITY.md](https://github.com/netbox-community/netbox-scripts/blob/main/COMPATIBILITY.md)
for the per-release compatibility matrix.

## Getting started

Once the release is published on PyPI, install it in your NetBox virtual
environment:

```bash
pip install netbox-scripts
```

For local development, run `pip install -e .` from the repository root instead.

Add `netbox_scripts` to `PLUGINS` in NetBox's `configuration.py`, keeping any
existing plugins:

```python
PLUGINS = [
    # Keep your existing plugins here.
    'netbox_scripts',
]
```

Configure source storage in the same file. This is required even when you use
the default plugin settings. For a single-node installation:

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

Keep any existing custom storage entries when adding this configuration.
NetBox merges it with its built-in storage entries. The plugin requires its
own storage entry and does not fall back to `default`.

Apply database migrations from the NetBox directory containing `manage.py`:

```bash
python manage.py migrate
```

All `PLUGINS_CONFIG` settings are optional. See the
[configuration guide](https://netbox-community.github.io/netbox-scripts/configuration/)
for available settings and object storage options, and the
[documentation](https://netbox-community.github.io/netbox-scripts/)
for the complete setup and usage guides.

## Feedback and support

Found a bug or something that is hard to use? Please
[open an issue](https://github.com/netbox-community/netbox-scripts/issues).
Include your NetBox and plugin versions, what you expected and what happened.
Feedback on installation, migration and documentation is especially helpful
during the alpha.

## License

Licensed under the [Apache License 2.0](https://github.com/netbox-community/netbox-scripts/blob/main/LICENSE).

Copyright (c) 2026 NetBox Labs.
