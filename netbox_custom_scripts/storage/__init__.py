"""
Project storage layer for the Custom Scripts plugin.

This package is plugin-internal and is deliberately not re-exported from the top-level
netbox_custom_scripts package, which surfaces only the authoring API. No module imports
the Django ORM. paths owns source-path safety, the on-disk layout, and directory walking
with bounded reads. manifest builds the content manifest and digest. Both read the
configured storage limits. A future service layer will own configuration resolution, ORM
transactions, and locking, and the directory walking may move to a dedicated store module
then.
"""
