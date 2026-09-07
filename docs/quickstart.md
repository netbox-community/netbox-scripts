# Quickstart

## Requirements

| Requirement | Version or note |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12 or newer |
| Extra services | None beyond a standard NetBox deployment, but its RQ worker has to be running. See [Background work](#background-work) |
| Project storage | A `STORAGES` entry, required before a Project can hold source. See [Configuring project storage](#configuring-project-storage) |

## Installing the plugin

Install into the NetBox virtualenv from the NetBox Labs artifact source:

```sh
pip install netbox-scripts
```

For a source checkout, run `pip install -e .` from the repository root
instead.

## Enabling the plugin

Add `netbox_scripts` to `PLUGINS` in NetBox's `configuration.py`:

```python
PLUGINS = [
    'netbox_scripts',
]
```

Everything in `PLUGINS_CONFIG` is optional and has a working default. See
[Configuration](configuration.md) for the full list. The storage backend below is
configured separately and is not optional.

Then run migrations, collect static files, and restart NetBox:

```sh
python manage.py migrate
python manage.py collectstatic --no-input
systemctl restart netbox netbox-rq
```

## Configuring project storage

Project source is written to the backend registered under the `netbox_scripts`
key of NetBox's `STORAGES` setting. Without it the plugin loads and its pages work, but
anything that stores source refuses, and the `netbox_scripts.W001` system check
reports it until the entry exists.

On a single node, Django's own `FileSystemStorage` is enough:

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

NetBox merges this with its built-in entries, so defining only this key leaves `default`
and the others intact. A horizontally scaled deployment points it at object storage
instead. See [Configuration](configuration.md) for that and for why the entry does not
fall back to NetBox's `default` storage.

## Background work

The plugin does its real work in background jobs, so NetBox's RQ worker has to be
running. Staging a revision, validating it, activating it, reconciling a Data Source
project, every script run, and every migration pass are all jobs.

With no worker, an uploaded revision stops at `materialized` and never reaches a verdict,
and a run requested over REST is refused with a 503 rather than queued for nobody.

## Verifying the install

| Check | Expected result |
|---|---|
| Visit `/plugins/` in NetBox | NetBox Scripts is listed |
| Open the navigation menu | A "Scripts" menu appears, with a Projects group and a Scripts group |
| `GET /api/plugins/netbox-scripts/` | Plugin API root responds |
| `python manage.py check` | No `netbox_scripts.W001`, meaning project storage is configured |
| Upload a script from *Scripts > Projects > Upload Script* | The Project's revision reaches `valid`, which proves storage and the worker are both working |

See [Uploading Scripts](uploading.md) for that last step in full.
