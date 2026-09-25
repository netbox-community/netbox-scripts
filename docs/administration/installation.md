# Installation

## Requirements

| Requirement | Version or note |
|---|---|
| NetBox | 4.7.0 to 4.7.99 |
| Python | 3.12 or newer |
| Extra services | None beyond a standard NetBox deployment. Keep its RQ worker running. See [Background work](#background-work). |
| Project storage | A `STORAGES` entry is required before a Project can hold source. See [Configuring project storage](#configuring-project-storage). |

## Installing the plugin

With your NetBox virtual environment active, install the plugin once its release
is available on PyPI:

```sh
pip install netbox-scripts
```

For a source checkout, run `pip install -e .` from the repository root instead.

## Enabling the plugin

Add `netbox_scripts` to `PLUGINS` in NetBox's `configuration.py`, keeping your
existing plugins:

```python
PLUGINS = [
    # Keep your existing plugins here.
    'netbox_scripts',
]
```

All `PLUGINS_CONFIG` settings are optional and have defaults. See
[Configuration](configuration.md). Project storage is configured separately
and is required.

## Configuring project storage

Add `netbox_scripts` to NetBox's `STORAGES` setting. Without it, the plugin loads,
but source-storage operations fail and the `netbox_scripts.W001` system check
reports the missing configuration.

For a single-node installation, use Django's `FileSystemStorage`:

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

NetBox merges this with its built-in entries, preserving `default` and the others.
If you already define `STORAGES`, add the key to that dictionary instead of
replacing it. For a multi-node deployment, see the shared-storage options in
[Configuration](configuration.md), which also explains why the plugin does not
fall back to `default`.

Create the directory before the first upload. Only the NetBox web and worker
accounts should be able to write to it. When both services run as `netbox`:

```sh
sudo install -d -m 0700 -o netbox -g netbox /var/lib/netbox-scripts
```

If they use different accounts, give both read and write access to source
storage. Keep each account's runtime cache private, as described in
[Runtime cache](configuration.md#runtime-cache).

## Applying the configuration

After configuring the plugin and storage, run migrations and collect static
files from the directory containing NetBox's `manage.py`. Use NetBox's virtual
environment, then restart the web and worker processes. For a standard installation:

```sh
source /opt/netbox/venv/bin/activate
cd /opt/netbox/netbox
python manage.py migrate
python manage.py collectstatic --no-input
sudo systemctl restart netbox netbox-rq
```

Use your deployment's restart procedure if these service names do not apply.
Processes must restart to load the new configuration.

## Background work

Keep NetBox's RQ worker running for validation, Data Source reconciliation,
queued Script runs and migration passes.

Uploads and manual activation also perform work in the web process.
`runcustomscript` executes in the process that invokes it.

Without a worker, an uploaded revision remains `materialized` and cannot finish
validation. REST run requests return HTTP 503.

## Verifying the install

| Check | Expected result |
|---|---|
| Visit `/plugins/` in NetBox | NetBox Scripts is listed. |
| Open the navigation menu | A **Scripts** menu contains Projects and Scripts groups. |
| `GET /api/plugins/netbox-scripts/` | The plugin API root responds. |
| `python manage.py check` | No `netbox_scripts.W001` warning. Project storage is configured. |
| Upload a script from *Scripts > Projects > Upload Script* | Successful validation leads to `active` with **Activate this upload** selected, or `valid` when it is cleared under the default Manual policy. |

See [Uploading Scripts](uploading.md) for the upload steps and results.
