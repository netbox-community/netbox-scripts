# Quickstart

## Requirements

| Requirement | Version or note |
|---|---|
| NetBox | 4.6.0 to 4.7.99 |
| Python | 3.12 or newer |
| Extra services | None |

## Installing the plugin

Install into the NetBox virtualenv from the NetBox Labs artifact source:

```sh
pip install netbox-custom-scripts
```

For a source checkout, run `pip install -e .` from the repository root
instead.

## Enabling the plugin

Add `netbox_custom_scripts` to `PLUGINS` in NetBox's `configuration.py`. The
plugin currently defines no `PLUGINS_CONFIG` settings.

```python
PLUGINS = [
    'netbox_custom_scripts',
]
```

Then run migrations, collect static files, and restart NetBox:

```sh
python manage.py migrate
python manage.py collectstatic --no-input
systemctl restart netbox netbox-rq
```

## Verifying the install

| Check | Expected result |
|---|---|
| Visit `/plugins/` in NetBox | NetBox Custom Scripts is listed |
| Open the navigation menu | A "Custom Scripts" menu with a "Custom Script Projects" entry appears |
| `GET /api/plugins/custom-scripts/` | Plugin API root responds |
