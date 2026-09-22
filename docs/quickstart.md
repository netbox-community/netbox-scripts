# Quickstart

## Requirements

| Requirement | Version or note |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12 or newer |
| Extra services | None beyond a standard NetBox deployment, but its RQ worker has to be running. See [Background work](#background-work) |
| Project storage | A `STORAGES` entry, required before a Project can hold source. See [Configuring project storage](#configuring-project-storage) |

## Installing the plugin

Once the release is published on PyPI, install it into the NetBox virtualenv:

```sh
pip install netbox-scripts
```

For a source checkout, run `pip install -e .` from the repository root
instead.

## Enabling the plugin

Add `netbox_scripts` to `PLUGINS` in NetBox's `configuration.py`, keeping any
existing plugins:

```python
PLUGINS = [
    # Keep your existing plugins here.
    'netbox_scripts',
]
```

Everything in `PLUGINS_CONFIG` is optional and has a working default. See
[Configuration](configuration.md) for the full list. The storage backend below is
configured separately and is not optional.

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
fall back to NetBox's `default` storage. Where your configuration already defines
`STORAGES`, add this key to that dictionary instead of assigning a second one.

Create that directory before the first upload. The NetBox web and worker processes both read
and write it, and nothing else should be able to write to it. For a standard installation where
both run as `netbox`:

```sh
sudo install -d -m 0700 -o netbox -g netbox /var/lib/netbox-scripts
```

Where they run as different users, give both of them read and write access instead.

## Applying the configuration

With both settings in place, run migrations and collect static files from the directory holding
NetBox's `manage.py`, with NetBox's virtual environment active, then restart NetBox so every
process loads them. In a standard installation:

```sh
source /opt/netbox/venv/bin/activate
cd /opt/netbox/netbox
python manage.py migrate
python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

Use your deployment's own restart procedure where those service names do not
apply. A process that has not restarted is still running without the storage
entry.

## Background work

Validating a revision, reconciling a Data Source project, a queued script run and
every migration pass are background jobs, so NetBox's RQ worker has to be running.
Uploading a revision and activating one by hand also do work in the web process,
and `runcustomscript` executes in the process that invoked it.

With no worker, an uploaded revision stops at `materialized` and never reaches a verdict,
and a run requested over REST is refused with a 503 rather than queued for nobody.

## Verifying the install

| Check | Expected result |
|---|---|
| Visit `/plugins/` in NetBox | NetBox Scripts is listed |
| Open the navigation menu | A "Scripts" menu appears, with a Projects group and a Scripts group |
| `GET /api/plugins/netbox-scripts/` | Plugin API root responds |
| `python manage.py check` | No `netbox_scripts.W001`, meaning project storage is configured |
| Upload a script from *Scripts > Projects > Upload Script* | The revision reaches `active`, since **Activate this upload** is ticked by default, which proves storage and the worker are both working. Clearing that tick leaves it at `valid` for manual activation |

See [Uploading Scripts](uploading.md) for that last step in full.
