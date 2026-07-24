"""
Project storage layer for the Custom Scripts plugin.

This package is plugin-internal. The top-level netbox_custom_scripts package surfaces only
the authoring API. config owns settings resolution. paths owns source-path safety and the
on-disk layout as pure functions of their arguments. manifest builds the content manifest
and the digest. store walks a source tree with bounded reads, writes and removes revision
directories, and owns filesystem errors. A future service layer will own ORM transactions
and locking, and will resolve the configuration it passes down.
"""
