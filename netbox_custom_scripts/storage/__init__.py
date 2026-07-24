"""
Project storage layer for the Custom Scripts plugin.

This package is plugin-internal and is deliberately not re-exported from the top-level
netbox_custom_scripts package, which surfaces only the authoring API. No module imports the
Django ORM. config owns settings resolution and is the only module that reads them. paths
owns source-path safety and the on-disk layout as pure functions of their arguments.
manifest builds the content manifest and the digest. store walks a source tree with bounded
reads and owns filesystem errors. A future service layer will own ORM transactions and
locking, and will resolve the configuration it passes down.
"""
