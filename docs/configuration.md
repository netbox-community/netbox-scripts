# Configuration

## Overview

NetBox Custom Scripts keeps project source in a Django storage backend, configured through
NetBox's `STORAGES` setting, and reads its remaining settings from the
`netbox_custom_scripts` entry in NetBox's `PLUGINS_CONFIG`. A deployment that
[uploads scripts](uploading.md) needs the storage entry, since that is where uploaded content
is written. One that only manages project definitions can defer the decision, and the
`netbox_custom_scripts.W001` system check reports it until it is made.

```python
PLUGINS_CONFIG = {
    'netbox_custom_scripts': {
        'max_project_size': 209715200,
    },
}
```

## Project storage

Stored revisions go to the backend registered under the `netbox_custom_scripts` key of
NetBox's `STORAGES` setting. **This entry is required.** Everything the plugin writes sits
under a single `netbox-custom-scripts/` prefix, so it stays separate from whatever else
that backend holds.

The entry is required rather than falling back to NetBox's `default` storage because a
revision is executable source, and it deserves a backend chosen for it rather than
inheriting the visibility, retention, and sharing policy of ordinary media. Pointing the
entry at the same physical backend as `default` is a legitimate decision, and writing it
out keeps that decision visible and lets the two diverge later. NetBox merges `STORAGES`
with its built-in entries, so a block that defines only this key leaves `default` and the
others intact.

### Local files

On a single node, Django's built-in `FileSystemStorage` stores revisions as ordinary
files:

```python
STORAGES = {
    'netbox_custom_scripts': {
        'BACKEND': 'django.core.files.storage.FileSystemStorage',
        'OPTIONS': {
            'location': '/var/lib/netbox-custom-scripts',
        },
    },
}
```

```text
/var/lib/netbox-custom-scripts/netbox-custom-scripts/<storage_key>/revisions/<digest>/hello.py
```

Deleting a Project or one of its revisions reclaims the files that revision recorded, not
the directories holding them, so an empty `<storage_key>/revisions/<digest>/` can remain
once a cleanup has completed. This is cosmetic, and specific to a filesystem backend: an
object store has no directory to leave behind. Removing such a directory by hand is safe,
and a revision that is later staged from identical content writes back into it.

Nothing here requires the `django-storages` package. `FileSystemStorage` ships with
Django, and `django-storages` is needed only for the object-store backends below.

### Object storage

A horizontally scaled deployment points the entry at an S3-compatible bucket:

```python
STORAGES = {
    'netbox_custom_scripts': {
        'BACKEND': 'storages.backends.s3.S3Storage',
        'OPTIONS': {
            'bucket_name': 'netbox-private-data',
            'location': 'custom-scripts',
            'default_acl': 'private',
        },
    },
}
```

S3 bounds the complete object key at 1024 UTF-8 bytes including every prefix. The plugin's
own key prefix uses 134 of them and an accepted source path uses at most 768, which leaves
122 bytes for the `location` above.

### The one requirement

**Every NetBox web and worker process must reach the same content.** A revision staged by
one process is executed by another. On a single node, a local directory satisfies that
and is the normal choice. On a horizontally scaled deployment, such as NetBox Enterprise
or NetBox Cloud, a per-pod local directory does not: a revision written by a web pod is
absent for the worker pod that has to run it, so those deployments need an object store or
a shared volume.

### Database connection pooling

**Custom Script Projects need session-mode pooling, or a database alias that is not pooled.**
Every operation that touches stored content serializes on a PostgreSQL session-level advisory
lock, and such a lock belongs to the physical backend connection that took it. Under
transaction-mode pooling, which pgbouncer offers and many deployments select, each transaction
can land on a different backend, so the acquire, the protected writes and the release drift
apart. Two callers can then both believe they hold one project's lock, or a lock can be left
behind until the pool recycles that connection, which stalls every later operation on that
Project.

### When the entry is missing or unusable

NetBox still boots, and everything unrelated to project storage keeps working. The
`netbox_custom_scripts.W001` system check reports the missing entry, and revision staging,
activation, and cleanup refuse with a configuration error until it is defined. A backend
that cannot be constructed is reported the same way when the storage layer uses it, rather
than at startup.

### Changing the backend later

The entry and its options are part of the deployment's persistent state: they say where
every stored revision lives. Changing the target backend, bucket, or `location` therefore
needs a coordinated move, not just a configuration edit. Stop staging and deletion activity,
let queued cleanup jobs drain, copy everything under the `netbox-custom-scripts/` prefix to
the new backend, and only then switch the entry. A cleanup job resolves the backend when it
runs, so a job enqueued before the switch would otherwise delete from the new backend while
its objects still sit in the old one. Verification on the next staging or activation
confirms the copied content arrived intact.

## Settings

| Setting | Type | Default | Meaning |
| --- | --- | --- | --- |
| `max_file_size` | positive integer (bytes) | 10485760 (10 MiB) | Largest accepted size for a single source file. |
| `max_project_size` | positive integer (bytes) | 104857600 (100 MiB) | Largest accepted total size of a project's source tree. |
| `max_file_count` | positive integer | 1000 | Largest accepted number of files in a project's source tree. |
| `runtime_cache_root` | path | system temporary directory | Directory the [runtime cache](#runtime-cache) materializes revision trees under. |

A limit set to a non-positive or non-integer value is rejected as a configuration error
when it is read.

## Source path policy

Source paths are also held to fixed limits, which are deliberately not configurable. They are
portability floors rather than capacity settings: a path that clears them stays writable,
walkable, and removable on every supported host, and importable by the package loader.

| Rule | Limit |
|---|---|
| Bytes in one path component, UTF-8 | 255 |
| Bytes in the whole relative path, UTF-8 | 768 |
| Directory levels | 64 |

A source file breaking any of these is rejected as content, with the codes
`path_component_too_long`, `path_too_long`, and `path_too_deep`, so the upload becomes an
inspectable invalid revision. Left to the filesystem these would surface later as
`ENAMETOOLONG` or descriptor exhaustion, recorded as an infrastructure failure that no retry
of the same content could ever clear.

Two names that differ only in letter case are rejected together under
`case_fold_conflict`, checked at every directory level rather than only in the full path, so
a tree holding both `Lib/deploy.py` and `lib/audit.py` is refused. The comparison is simple
case mapping, matching what APFS, NTFS, and HFS+ do, so names those hosts keep apart stay
usable.

Compiled Python files and `__pycache__` directories are rejected under `compiled_artifact`.
Compiled bytecode imports without the source anyone would review, so it is not project
source.

## Runtime cache

Python imports need a real directory tree, so before a revision loads, its files are
materialized from the storage backend into a local directory:

```text
<runtime_cache_root>/<storage_key>/<digest>/
```

The default root sits under the system temporary directory
(`<tempdir>/netbox-custom-scripts/runtime-cache`), which is per-process-host, writable,
and disposable. Set `runtime_cache_root` to place it elsewhere, for example on a larger
or faster volume. Whatever the location, the cache is scratch state: losing it costs a
rebuild from the backend, never data, so nothing about it needs to be backed up or
shared between nodes. Per-pod scratch space is exactly right on a horizontally scaled
deployment.

Every directory level the plugin creates is created private to the account NetBox runs as, and
materialization refuses a root whose ancestors another account could rename. An ancestor
writable by other users is accepted only when it is sticky, which is what keeps the shared
temporary directory usable. Verification proves what a tree held when it was checked, and it
cannot prove that no one swapped the directory afterwards, which is why placement is checked at
all.

A directory created outside the plugin does not get that treatment, so a `runtime_cache_root`
prepared by hand under a permissive umask is the common way to hit this. Validation then fails
as an environment error naming the offending directory, and the fix is to restrict it:

```bash
chmod 700 /path/to/runtime-cache
```

Every ancestor is checked, so a private cache directory inside a group-writable parent is still
refused. Point `runtime_cache_root` somewhere already private, or restrict the parent too.

A cached tree is never trusted because it exists. Every use re-verifies it against the
revision manifest, and the protocol is built so nothing unverified can execute:

- Compiled Python files are removed before any verification, because a planted one can
  be flagged to skip its own source check. Verification then requires exactly the
  manifest's files and nothing else, symbolic links included.
- A tree that fails verification is set aside next to its slot rather than repaired in
  place, and a fresh tree is rebuilt from the backend through the same size-preflighted,
  bounded, checksummed reads the storage layer uses everywhere.
- A rebuilt tree is staged as a sibling of its final location, verified as a whole,
  published with one rename, and write-protected before it is served, so a reader only
  ever sees a complete, just-verified, read-only tree. A tree left unprotected by an
  interrupted build is protected again the next time it is served. Concurrent builders
  of one revision serialize on a per-slot file lock that the operating system releases
  if the process dies.

The root must offer enough space for the revisions in active use, and staging happens
beside the final location, so the root must be one filesystem. There is no automatic
eviction: reclaiming stale slots and set-aside failures belongs to a housekeeping
reconciler planned for a later release, until then the directory can be cleared out of
band while NetBox is stopped.

## Storage trust boundary

The project storage backend and the runtime cache directory are trusted inputs to code
execution.
Treat write access to either as equivalent to running code as the NetBox service account,
and size the backend and filesystem permissions accordingly.

- **Only the NetBox service identity, or a trusted deployment identity, may write to the
  storage backend or to the runtime cache directory.** Anything else with write access can
  place code where NetBox will later import it.
- **Content is trusted because it matches its manifest, not because of where it sits.**
  Every stored file is checked against its recorded size and SHA-256 on every path that
  returns a revision, including immediately after it is written. Verification asks the
  backend for an object's reported size before opening it, so an object replaced with
  something larger is rejected without a download, and a backend that reports no sizes
  falls back to a read bounded at one byte past the recorded size. Only the keys the
  manifest names are ever read, materialized, or executed, so an unexpected key under a
  revision is inert rather than importable.
- **A manifest is verified immediately before import, not once at write time.** Content
  that changed after it was staged does not match its recorded checksum and is rejected
  then.
- **A cache entry is not trusted merely because it exists.** The runtime cache is
  rebuildable state, so an entry that fails verification is discarded and repopulated from
  the backend.
- **Give the backend its own bucket, container, or directory** where the deployment allows
  it. The plugin confines itself to one prefix, but a backend shared with unrelated writers
  widens who can put content where NetBox will look for it.

Custom Script Project and Revision rows, and the cleanup jobs that reclaim their stored
content, live on the default database, and the whole storage lifecycle is bound to it.
The core job API records a cleanup Job and its queue handoff on the default connection,
so staging, activation, and deletion arriving on any other database alias are refused up
front, rather than allowed to create content whose deletion could never record its
cleanup. The cleanup job also repeats the branching routing check when it runs, because
it may execute much later or on another pod, and it fails while leaving content in place
rather than trust an answer that is no longer safe. NetBox Branching's schema routing on
the default connection is fully supported, arbitrary secondary databases are not.

## What the backend does and does not guarantee

Moving the authoritative store to a Django storage backend is what makes it work on a
horizontally scaled deployment, and it changes what the plugin can promise about tampering.

Writing cannot go through a symbolic link planted at a key, because a filesystem backend
creates files with `O_CREAT` and `O_EXCL` and the plugin refuses a key the backend renames.
Removal unlinks the name it is given rather than following it. Both statements hold at the
final key only: on a filesystem backend, a directory component under the storage root that
is replaced with a symbolic link redirects everything below it, writes included. The plugin
does not defend against mutation of the backend's own tree, which is what the trust
boundary above is for. Whoever can restructure the storage root can already place code, so
write access to it stays confined to the trusted identities. Reading is where the
difference lies: a backend resolves a key however it chooses, and the plugin does not
control that resolution, so a redirected read is caught by the checksum rather than
prevented. Content that does not hash to what the manifest recorded is rejected, which
means a redirect can deny service but cannot substitute code.

This is the only guarantee available once the store may be an object store, and it applies
uniformly to every backend rather than only to a local filesystem.

## Reclaiming stored content

Deleting a Project or a revision records a cleanup Job carrying the exact keys to remove, in the
same transaction that deletes the row, so the intent to reclaim survives even if the queue never
picks the job up. Deletion is by exact key and tolerates content that is already gone, which is
what makes a retry safe.

A daily storage sweep runs as a background system job and reports what a cleanup did not finish.
It rechecks each unfinished cleanup under the project lock and records four groups on its own Job
row: content still in the store and named by no revision, content already reclaimed, content a
later revision references again, and cleanups whose payload or backend could not be read. The
first group is also logged as a warning naming the storage key and digest. Several cleanup Jobs
can name one stored tree, since a Project cascade records one per revision, so the report counts
trees rather than Jobs.

The pass is bounded by `RQ_DEFAULT_TIMEOUT`, five minutes unless a deployment raises it, and a
background system job cannot set a timeout of its own. It therefore takes the oldest stalled
cleanups first and records the report as it goes, so a very large backlog yields a truncated
report naming the most stale content rather than no report at all. The next day's run starts
again from the oldest.

The sweep reclaims nothing. Removing content a row might still name cannot be undone, so
reclamation stays a deliberate step an operator takes after reading the report. Content that no
cleanup Job names at all is outside what the sweep can see, because finding it means enumerating
the backend and the storage contract does not require a backend that can list. Content addressing
also means such a stray heals itself: identical bytes uploaded later resolve to the same key and
the new revision adopts them.

## NetBox Branching

The plugin runs alongside NetBox Branching, and this section records what a branch does and
does not change. The routing and execution statements below are verified against NetBox Branching
v1.2.0-beta1 by `netbox_custom_scripts/tests/test_branching_provisioned.py`, which provisions a
real branch.

Custom Script Projects, Modules, Custom Scripts, revisions and migration runs are
installation-global. The plugin routes all five to the main schema, so a Project edited
inside a branch applies everywhere at once, appears in no branch diff, and is neither
replayed by a merge nor rolled back by reverting one. None of their tables is replicated
into a branch schema at all.

That is deliberate. A revision names its stored source by the project's storage key and its
own content digest, and that path carries no schema component, so two schemas holding rows
for one tree would let a deletion in one reclaim source the other still serves.

Tags and journal entries on those objects stay branch-local. A tag assignment is branch-aware
ahead of any exemption, so no configuration can make one global, while a journal entry is
branch-aware by the ordinary change-logging rule. Tagging a Project inside a branch is therefore
visible only in that branch, which matches how NetBox's own exempt models behave.

**A run always writes to the main schema, whatever branch the requesting user had active.**
This is enforced rather than incidental. A Job carries a copy of the request that asked for the
run, a branch selection travels on that copy, and NetBox Branching reactivates it on the worker,
so the run stands that branch down before the script is called.

The alternative was rejected on what it does to a recurring run. A schedule re-enqueues with the
same request, so a run following the branch would keep writing to it for as long as it stayed
ready, then move to the main schema without a word the day it was merged, with nothing recording
which schema any earlier occurrence had used. A Custom Script cannot be used to stage changes
inside a branch, and that is deliberate.
