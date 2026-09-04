# Compatibility

This document tracks the supported NetBox and Python versions for each
release of NetBox Scripts.

| Plugin Version | Minimum NetBox Version | Maximum NetBox Version | Minimum Python |
|----------------|------------------------|------------------------|----------------|
| 0.0.1 | 4.7.0 | 4.7.99 | 3.12 |

## Notes

| Note | Action |
|---|---|
| NetBox upgrade | Test against the target NetBox version before production rollout. |
| Upstream changes | Review the [NetBox release notes](https://docs.netbox.dev/en/stable/release-notes/). |
| Support range change | Add a matrix row and update `PluginConfig.min_version` / `max_version`. |

## Upgrading

When upgrading either NetBox or this plugin:

1. Review the matrix above for the target combination.
2. Back up the NetBox database.
3. Install the new release of the plugin alongside (or after) the new NetBox release.
4. Apply database migrations:
   ```bash
   python manage.py migrate
   ```
5. Clear the cache:
   ```bash
   python manage.py clearcache
   ```
