# Configuration

## Overview

Configure Project source storage in NetBox's `STORAGES` setting. Optional plugin
settings belong under `PLUGINS_CONFIG['netbox_scripts']`.

You can create Project definitions before configuring storage. The
`netbox_scripts.W001` system check reports the missing entry until it is set.
Storage is required for operations such as [uploading scripts](uploading.md).

```python
PLUGINS_CONFIG = {
    'netbox_scripts': {
        'max_project_size': 209715200,
    },
}
```

## Project storage

Revisions use the backend registered as `STORAGES['netbox_scripts']`.
**This entry is required.** The plugin stores its content under a
`netbox-scripts/` prefix, separate from other content in the backend.

**Treat write access to this storage as equivalent to running code as the NetBox
service account.** The plugin executes source from it after checking the content
against a database-held manifest. Read
[Storage trust boundary](#storage-trust-boundary) before choosing a backend.

The plugin does not fall back to `default`. Its separate entry lets you choose
access, retention and sharing settings for executable source independently of
ordinary media. Both entries may use the same physical backend.

NetBox merges `STORAGES` with its built-in entries. Defining only this key leaves
`default` and the other built-in entries intact.

### Local files

On a single node, Django's `FileSystemStorage` stores revisions as ordinary files:

```python
STORAGES = {
    'netbox_scripts': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {
            'location': '/var/lib/netbox-scripts',
        },
    },
}
```

Create the directory before the first upload. It must be writable by the web and
worker accounts, but not by other users. See
[Quickstart](quickstart.md#configuring-project-storage) for an example.

Stored files follow this layout:

```text
/var/lib/netbox-scripts/netbox-scripts/<storage_key>/revisions/<digest>/hello.py
```

Deleting a Project or revision removes its recorded files, not their directories.
An empty `<storage_key>/revisions/<digest>/` directory may remain. Empty directories
can be removed manually. Staging identical content later recreates the needed path.
Object stores do not have these directories.

`FileSystemStorage` is included with Django. Install `django-storages` only when
using an object-storage backend that requires it.

### Object storage

For a multi-node deployment, an S3-compatible backend can provide shared storage:

```python
STORAGES = {
    'netbox_scripts': {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': 'netbox-private-data',
            'location': 'netbox-scripts',
            'default_acl': 'private',
        },
    },
}
```

S3 limits complete object keys to 1024 UTF-8 bytes. The plugin's prefix uses
127 bytes, and an accepted source path uses at most 768. This leaves 129 bytes
for the backend's `location` prefix.

### The one requirement

**Every NetBox web and worker process must have access to the same stored content.**
Source staged by one process can be executed by another.

A local directory is suitable on a single node. On a multi-node deployment,
including NetBox Enterprise or NetBox Cloud, use object storage or a shared volume.
A directory local to one pod is not available to a worker in another pod.

### Database connection pooling

**Connect the `default` database directly or through a session-mode pooler.**
Transaction-mode pooling is not supported for Script Project storage operations.

These operations use PostgreSQL session-level advisory locks. Acquiring the lock,
performing the protected work and releasing it must use the same database
connection. Transaction-mode pooling can move transactions between connections,
allowing overlapping operations or leaving locks behind that block later work.

### When the entry is missing or unusable

NetBox can start without the storage entry. Features unrelated to source storage
continue to work, but staging, activation and cleanup fail with a configuration
error. The `netbox_scripts.W001` check reports the missing entry.

If the configured backend cannot be constructed, the error is reported when the
storage layer uses it rather than at startup.

### Changing the backend later

Changing the backend, bucket or `location` requires moving the stored source,
not just editing configuration:

1. Stop staging and deletion activity, and let queued cleanup Jobs finish.
2. Copy all content under the `netbox-scripts/` prefix to the new backend.
3. Switch the storage entry to the new location.

A cleanup Job resolves the backend when it runs. Switching too early could make
it delete from the new backend while its original content remains in the old one.
Verification during later staging or activation checks the copied content.

## Settings

| Setting | Type | Default | Meaning |
|---|---|---|---|
| `max_file_size` | positive integer (bytes) | 10485760 (10 MiB) | Largest accepted size of one source file. |
| `max_project_size` | positive integer (bytes) | 104857600 (100 MiB) | Largest accepted total source-tree size. |
| `max_file_count` | positive integer | 1000 | Largest accepted number of source files. |
| `runtime_cache_root` | path | system temporary directory | Root used to materialize revision trees. See [Runtime cache](#runtime-cache). |

**`max_project_size` limits accepted content, not peak memory use.** Staging builds
the entire candidate tree in memory before checking its total size. An oversized
tree therefore consumes memory before it is rejected.

An upload holds the existing tree plus the new file. One call can reach
`max_project_size` plus `max_file_size` before the total-size check. Both upload
surfaces perform this work in the web process. The create and Add Script forms
use a commit hook, while REST stages within the request.

Data Source staging reads the path inventory first, then fetches content only
under the Project's `data_path`. A repository containing several Projects does
not load all their content for each Project. This staging, including migration
staging, runs in an RQ worker.

Verification and Script File refresh read one file at a time rather than holding
the entire tree. Non-positive or non-integer size/count settings raise a
configuration error when read.

## Source path policy

Source paths have fixed portability limits. These are not configurable capacity
settings.

| Rule | Limit |
|---|---|
| Bytes in one path component, UTF-8 | 255 |
| Bytes in the whole relative path, UTF-8 | 768 |
| Directory levels | 64 |

A source path that exceeds a limit produces an invalid revision with
`path_component_too_long`, `path_too_long` or `path_too_deep`. Rejecting it as content
avoids a later filesystem or descriptor error that retrying the same source would
not resolve.

Case-only name conflicts are rejected with `case_fold_conflict`. The comparison
applies at every directory level, so `Lib/deploy.py` and `lib/audit.py` cannot share
one tree. Simple case mapping follows APFS, NTFS and HFS+ behavior without rejecting
names those filesystems distinguish.

Compiled Python files and `__pycache__` directories are rejected with
`compiled_artifact`. Project source must remain available for review rather than
being supplied only as executable bytecode.

## Runtime cache

Python imports require a directory tree. Before loading a revision, the plugin
materializes its source from storage into a local cache:

```text
<runtime_cache_root>/<storage_key>/<digest>/
```

The default root is `<tempdir>/netbox-scripts/runtime-cache` on each host.
Set `runtime_cache_root` to use another location, such as a larger or faster volume.
The cache can be rebuilt from Project storage. It does not need backups or sharing
between nodes. Per-pod temporary storage is suitable for multi-node deployments.

Web and worker processes that run as different accounts on one host and share a
temporary directory each need their own private `runtime_cache_root`. NetBox's
shipped systemd units already give each service a private `/tmp`. Do not make
the cache group-writable to share it.

The plugin creates private cache directories and checks their parent directories.
A parent writable by other users is accepted only when its sticky bit prevents
those users from renaming the cache directory. This permits use of the shared
system temporary directory without relying on content verification alone.

A directory created manually does not automatically receive those permissions.
An unsafe root or parent makes validation fail with an environment error naming
the directory. Restrict its access:

```bash
chmod 700 /path/to/runtime-cache
```

Every parent is checked, so a private cache directory inside a group-writable,
non-sticky parent is still rejected. Restrict the parent or choose a safe location.

Existing cache content is verified on every use:

- Compiled files are removed before verification. The remaining tree must contain
  exactly the manifest's files, with no unexpected files or symbolic links.
- A failed tree is set aside rather than repaired in place. Its replacement is
  fetched using size-checked, bounded reads and checksum verification.
- The replacement is built beside its final location, verified, published with
  one rename and write-protected before use. Interrupted protection is restored
  on the next use. Concurrent builds of one revision use a per-slot file lock
  that the operating system releases if the process dies.

Provide space for active revisions and their temporary sibling trees on one
filesystem. There is no automatic eviction or cleanup of stale and failed slots.
Remove them outside the plugin while NetBox is stopped.

## Storage trust boundary

**Only the NetBox service identity or a trusted deployment identity should be
able to write Project storage or the runtime cache.** Treat that access as code
execution under the NetBox service account.

The plugin verifies content against the database-held manifest, not its location.
Each file's recorded size and SHA-256 are checked, including after a write and
immediately before import. When a backend reports sizes, an oversized object is
rejected before download. Otherwise, reads are bounded to one byte beyond the
recorded size.

Only manifest keys are read, materialized and executed. Unexpected storage keys
are not imported. A cache entry that fails verification is rebuilt from storage.
These checks do not replace restricting write access to the backend and cache.

Use a dedicated bucket, container or directory where possible. The plugin keeps
its own prefix, but sharing a backend with unrelated writers expands access to
the location holding executable source.

The storage lifecycle uses the default database. Project and revision rows, as
well as their cleanup Jobs, must remain on that connection. Staging, activation
and deletion on another database alias are rejected because cleanup cannot be
recorded consistently there.

Cleanup repeats the branching-routing check when it runs, even if it was queued
on another pod or much earlier. Unsafe routing leaves content in place and fails
the Job. NetBox Branching's schema routing on the default connection is supported.
Arbitrary secondary databases are not.

## What the backend does and does not guarantee

Checksums detect changed content, but the storage backend still controls how its
keys resolve. The plugin does not prevent every change to a backend's directory
structure.

At the final key, filesystem creation with `O_CREAT` and `O_EXCL` avoids following
a planted symbolic link. The plugin rejects a renamed save result. Removal unlinks
the final name rather than following it.

These protections do not cover a parent directory replaced by a symbolic link.
That can redirect reads and writes below it. Restrict who can modify the storage
root, as described in [Storage trust boundary](#storage-trust-boundary).

A redirected read is checked against the manifest. Different content is rejected,
so redirection can make source unavailable but cannot substitute content that
fails its recorded checksum. Content checks apply across both local and remote
backends.

## Reclaiming stored content

Deleting a Project or revision records a cleanup Job in the same database
transaction. The Job carries the exact keys to remove, so cleanup intent survives
even if queue delivery fails. Removing an already-absent key is safe to retry.

A daily storage sweep reports unfinished cleanup without deleting content. It
rechecks cleanup work under the Project lock and records four categories:

- Stored trees no revision references, also logged as warnings with their storage
  key and digest.
- Content already reclaimed.
- Content referenced again by a later revision.
- Cleanup payloads or backends that could not be read.

The report counts trees rather than Jobs because several cleanup Jobs can refer
to the same stored tree.

The sweep is bounded by `RQ_DEFAULT_TIMEOUT`, five minutes unless changed by the
deployment. A background system Job cannot set its own timeout. The sweep starts
with the oldest stalled cleanup and saves progress as it runs, so a large backlog
can produce a partial report. The next run starts from the oldest work again.

Review the report before reclaiming content. The sweep cannot find content absent
from every cleanup Job, because storage backends are not required to list their
contents. Staging identical content later can reuse those storage keys.

## NetBox Branching

NetBox Scripts can run alongside NetBox Branching. The routing and execution
behavior below is covered against NetBox Branching v1.2.0-beta1 by the real-branch
regression in `netbox_scripts/tests/test_branching_provisioned.py`.

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

**Script execution always writes to the main schema**, regardless of the
requester's active branch. The run explicitly clears branch context restored
from the copied request before calling the Script.

A recurrence reuses its request, so following that request's branch could change
its write destination after the branch is merged. This plugin does not support
using Script execution to stage changes inside a branch.
