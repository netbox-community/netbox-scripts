# Compatibility

This matrix records the NetBox and Python requirements for each NetBox Scripts
version. Use a Python version supported by both the plugin and your NetBox release.

| Plugin Version | Minimum NetBox Version | Maximum NetBox Version | Minimum Python |
|----------------|------------------------|------------------------|----------------|
| 0.0.1 | 4.7.0 | 4.7.99 | 3.12 |

> [!WARNING]
> NetBox Scripts is in alpha and is not recommended for production use. Test
> installation, migration and upgrades in a non-production environment first.
> The compatibility range is not a guarantee of a seamless alpha upgrade.

## Notes

The maximum NetBox version is the plugin's declared upper bound, not a list of
patch releases already tested. The test matrix in
[`.github/workflows/test.yml`](.github/workflows/test.yml) identifies the refs
checked by CI. Moving branches help detect upcoming changes but do not extend the
supported range.

When changing support, maintainers update this matrix, the `min_version` and
`max_version` declarations in `netbox_scripts/__init__.py`, and the CI matrix.
Python requirements live in `pyproject.toml`.

## Upgrading

1. **Check the target versions.** Read the plugin's
   [release notes](https://netbox-community.github.io/netbox-scripts/releases/)
   and the [NetBox release notes](https://docs.netbox.dev/en/stable/release-notes/).
   Check every installed plugin before changing NetBox.
2. **Prepare a recovery point.** Back up the database, configuration and source
   storage together, and record the installed versions. Review pending Script
   runs and follow any release-specific worker instructions. Database and source
   backups do not restore queued input or undo external actions.
3. **Upgrade using your deployment's procedure.** For a NetBox upgrade, follow
   the [NetBox upgrade guide](https://docs.netbox.dev/en/stable/installation/upgrading/).
   Install compatible plugin versions in the same environment before running
   upgrade tasks. Keep the plugin in the deployment's dependency list so rebuilding
   the environment does not remove it.
4. **Apply and verify the change.** For a plugin-only upgrade, follow
   [Quickstart](https://netbox-community.github.io/netbox-scripts/quickstart/#applying-the-configuration)
   for migrations, static files and restarting web and worker processes. Check
   NetBox's system checks, Project status and a suitable test Script before
   resuming normal operation.

Moving from built-in Custom Scripts is a separate operation. Read the
[migration guide](https://netbox-community.github.io/netbox-scripts/migration/)
before starting it. Do not use queue deletion or a Redis flush as a routine
upgrade step.
