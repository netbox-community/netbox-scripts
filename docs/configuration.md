# Configuration

## Overview

NetBox Custom Scripts reads its settings from the `netbox_custom_scripts` entry in
NetBox's `PLUGINS_CONFIG`. The settings below govern where project source is stored and
how large a source tree may be. The storage layer that reads them ships in this
pre-alpha release, but no user-facing way to stage a revision does yet, so a deployment
that only manages project definitions can leave them unset. The plugin boots without the
two path settings, and each path is validated the first time a storage function needs it
rather than at startup.

```python
PLUGINS_CONFIG = {
    'netbox_custom_scripts': {
        'project_root': '/opt/netbox-custom-scripts/projects',
        'runtime_cache_root': '/opt/netbox-custom-scripts/cache',
    },
}
```

## Settings

| Setting | Type | Default | Meaning |
| --- | --- | --- | --- |
| `project_root` | absolute path | unset | Directory that holds every project's stored revisions. Must already exist and be readable, writable, and traversable. This is shared storage. The same directory must be reachable by every NetBox web and worker process. |
| `runtime_cache_root` | absolute path | unset | Directory where a revision's source tree is materialized for execution. Must already exist and be readable, writable, and traversable. It may be node-local, so each worker can keep its own cache. |
| `max_file_size` | positive integer (bytes) | 10485760 (10 MiB) | Largest accepted size for a single source file. |
| `max_project_size` | positive integer (bytes) | 104857600 (100 MiB) | Largest accepted total size of a project's source tree. |
| `max_file_count` | positive integer | 1000 | Largest accepted number of files in a project's source tree. |

The two path settings default to unset and are validated lazily. A limit set to a
non-positive or non-integer value is rejected as a configuration error when it is read.

## Source path policy

Source paths are also held to fixed limits, which are deliberately not configurable. They are
portability floors rather than capacity settings: a path that clears them stays writable,
walkable, and removable on every supported host, and importable by the package loader.

| Rule | Limit |
|---|---|
| Bytes in one path component, UTF-8 | 255 |
| Bytes in the whole relative path, UTF-8 | 1024 |
| Directory levels | 64 |

A source file breaking any of these is rejected as content, with the codes
`path_component_too_long`, `path_too_long`, and `path_too_deep`, so the upload becomes an
inspectable invalid revision. Left to the filesystem these would surface later as
`ENAMETOOLONG` or descriptor exhaustion, recorded as an infrastructure failure that no retry
of the same content could ever clear.

## Storage trust boundary

Both storage roots are trusted inputs to code execution. Treat write access to them as
equivalent to running code as the NetBox service account, and size the filesystem
permissions accordingly.

- **Only the NetBox service identity, or a trusted deployment identity, may write to
  `project_root` and `runtime_cache_root`.** Anything else with write access can place
  code where NetBox will later import it.
- **Make an active revision directory read-only after activation** where the deployment
  allows it. A revision is immutable by design, so nothing should rewrite it.
- **A manifest is verified immediately before import, not once at write time.** A file
  that changed on disk after it was staged does not match its recorded checksum and is
  rejected then.
- **A cache entry is not trusted merely because its directory exists.** The runtime cache
  is rebuildable state, so an entry that fails verification is discarded and repopulated
  from `project_root`.
- **No symbolic link is permitted anywhere below `project_root`.** Reading, writing,
  verifying, and removing a stored revision all refuse one, so a link cannot redirect an
  operation to content the plugin does not own. A refused removal is logged and the content
  is left for an operator rather than followed.

`project_root` is shared storage and must be reachable by every web and worker process.
`runtime_cache_root` may be node-local, so each worker can keep its own copy.

`project_root` itself may be a symbolic link, since pointing it at a mounted volume is a
normal operator choice. The rule above applies to everything the plugin creates beneath it.

Storage cleanup and revision activation run on the database connection that performed the
matching write, so both stay correct on a deployment where a plugin routes these models to a
connection of its own.

## Platform requirement

Project storage requires a POSIX filesystem. Every path component below `project_root` is
opened through a directory descriptor that refuses a symbolic link, which relies on
`O_NOFOLLOW`, `O_DIRECTORY`, and descriptor-relative operations, and that is how a link
swapped in between validation and use is defeated. The package is classified
`Operating System :: POSIX` for that reason.
