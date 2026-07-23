# Configuration

## Overview

NetBox Custom Scripts reads its settings from the `netbox_custom_scripts` entry in
NetBox's `PLUGINS_CONFIG`. The source-storage features are not yet exposed in this
pre-alpha release. The settings below establish the storage contract for the upcoming
project revisions. The plugin boots without the two path settings, but source staging
stays unavailable until they are configured, and each path is validated the first time a
storage function needs it rather than at startup.

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
