"""
Project storage layer for the NetBox Scripts plugin.

This package is plugin-internal. The top-level netbox_scripts package surfaces only
the authoring API. config owns settings resolution and picks the storage backend. paths owns
source-path safety and the key layout as pure functions of their arguments. manifest builds
the content manifest and the digest. store writes, verifies, and removes revision content
through that backend, and owns its errors. locks hands out the per-project serialization lock
every content operation holds. service resolves the configuration for one operation and owns
the ORM transactions and row locks that stage and activate a revision.
"""
