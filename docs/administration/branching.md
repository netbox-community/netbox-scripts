# NetBox Branching

NetBox Scripts can run alongside NetBox Branching. The following rules apply
when using both plugins.

!!! warning "Scripts do not run inside branches"

    Script runs use the main schema, regardless of your active branch. They
    cannot be used to stage changes inside a NetBox Branching branch.

## Installation-wide objects

Projects, Script Files, Scripts, revisions and migration runs are installation-wide.
All five use the main schema. Changes made from a branch apply everywhere and do
not appear in its diff, merge or revert. Their tables are not copied into branch
schemas.

Stored source uses the Project's storage key and revision digest without a schema
component. Keeping these objects global prevents a deletion in one schema from
removing source another schema still serves.

Tags and journal entries on these objects remain branch-local, like those on
NetBox's other exempt models. Tag assignments are branch-aware before exemptions
apply, and cannot be made global through configuration. Journal entries follow
the usual branch-aware change-logging rule.

## Exempting the models

NetBox Branching's `exempt_models` setting declares that scope. Add these model
names to the existing `exempt_models` list under
`PLUGINS_CONFIG['netbox_branching']`. Keep any other plugin settings and
exemptions already configured.

```python
PLUGINS_CONFIG = {
    'netbox_branching': {
        'exempt_models': [
            'netbox_scripts.migrationrun',
            'netbox_scripts.netboxscript',
            'netbox_scripts.scriptfile',
            'netbox_scripts.scriptproject',
            'netbox_scripts.scriptprojectrevision',
        ],
    },
}
```

List the models explicitly rather than using `netbox_scripts.*`. A wildcard
would also exempt future models that may need ordinary branching behavior.

The plugin also attempts to register a resolver that routes these models to the
main schema. The explicit settings provide a fallback when that resolver is
unavailable. Neither mechanism replaces the routing check before storage work.

## Routing check

Before staging, activation or stored-source removal, the plugin checks routing
through NetBox Branching's public API:

- Confirmed main-schema routing allows the operation.
- Unsafe or unverifiable routing refuses the storage operation, and a system
  check explains why. Configure exemptions when needed. If routing cannot be
  inspected, use a NetBox Branching release that provides the inspection API.
  Exemptions alone cannot make an unavailable check succeed.
- When a Project or revision row is deleted but cleanup cannot confirm safe
  routing, source is retained and the refusal is logged at error level.

NetBox remains available. These refusals affect the plugin's storage operations,
not the rest of the installation.

## Script execution

The run explicitly clears branch context restored from the copied request before
calling the Script.

A recurrence reuses its request, so following that request's branch could change
its write destination after the branch is merged. This plugin does not support
using Script execution to stage changes inside a branch.
