# Change Log

## v0.0.1 (2026-09-25)

* Initial release.

!!! warning "Alpha release"

    NetBox Scripts v0.0.1 is an alpha for testing and is not recommended for
    production use. Behavior, defaults and APIs can change before the beta.

    - **There is no upgrade path from this alpha to the beta.** The beta
      replaces the alpha's database schema, so plan to remove the alpha before
      installing it. Projects, revisions and run history created with the alpha
      do not carry over.
    - **Rehearse the migration from NetBox's built-in Custom Scripts on a
      copy.** The migration is one-way, and removing the plugin restores
      nothing. Once you run the alpha's cutover, restoring the backup taken
      before it is the only clean way to migrate again with the beta.
    - **Known bugs** are tracked as
      [open bug reports](https://github.com/netbox-community/netbox-scripts/issues?q=is%3Aissue+is%3Aopen+label%3A%22type%3A+bug%22).

See [Features](features.md) for supported features and limitations, and
[Installation](administration/installation.md) for setup. Read [Migration](administration/migration.md)
before moving from NetBox's built-in Custom Scripts.
