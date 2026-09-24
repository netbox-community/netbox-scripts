# NetBox internals this plugin depends on

This reference records the NetBox integration points and supporting runtime
dependencies the plugin relies on. Use it to locate affected code when upgrading
NetBox. Interfaces outside NetBox's documented plugin API may change without
warning.

The table also includes Django and third-party dependencies, plus documented
interfaces whose behavior matters to the integration. It is a dependency ledger,
not a list of unsupported APIs alone.

## The list

| Symbol | Where we use it | What it gives us |
|---|---|---|
| `netbox.registry.registry['request_processors']` | `execution.py` | Applies the registered request context managers around Script execution. |
| `netbox.context_managers.event_tracking` | `execution.py` | Provides change attribution and event queuing. Skipped for dry runs. |
| `netbox.context.current_request` | `execution.py`, `forms/model_forms/projects.py` | Preserves and restores the request context after successful, failed or nested runs. Gives the Project forms their acting user without storing the request on the instance, which each queued event passes to `EVENTS_PIPELINE` consumers that may pickle it. |
| `core.signals.clear_events` | `execution.py` | Discards pending events when a run is abandoned. |
| `django.db.router.db_for_write` on a change-logged core model | `execution.py` | Resolves the database for change-logged writes, including branch routing. `execution.py` checks its probe model with NetBox Branching first, so a model that stopped being branch-aware is reported rather than read as the default alias. |
| `utilities.exceptions.AbortScript` | `execution.py` | Handles the abort raised by unchanged built-in Custom Scripts alongside the plugin's own abort. Documented for Script authors, not plugins, and expected to leave with the built-in feature at v5.0. |
| `utilities.request.copy_safe_request` | `views/scripts.py`, `api/views.py` | Copies the request for a worker and strips sensitive headers. The copy must be picklable. |
| `netbox.api.viewsets.mixins.discard_events_on_rollback` | `api/views.py` | Discards request events when a declaration-writing API action rolls back. Never used for disposable-write permission probes. |
| `netbox.api.viewsets.NetBoxModelViewSet.perform_update` and `perform_destroy` | `api/views.py` | Lets the lock-order mixin acquire the Project lock before NetBox's conditional row lock. That row lock must remain inside these hooks. The canary detects a missing hook, not a change in where it takes the lock. |
| `utilities.exceptions.PermissionsViolation` | `permissions.py`, `api/views.py` | Rejects child writes outside the user's object-permission scope through NetBox's enclosing rollback path. |
| `utilities.exceptions.AbortRequest` | `models/projects.py`, `forms/model_forms/projects.py` | Refuses a form or model save. REST returns 400, and the UI displays the message on the form. Documented in `docs/plugins/development/exceptions.md` and handled by `NetBoxModelViewSet.dispatch`, `ObjectEditView.post` and both bulk views. |
| `netbox.views.generic.BulkEditView.pre_save_operations` | `views/projects.py` | Runs the source-field permission check before each save in NetBox's bulk-edit transaction. The check reads the undocumented `_nullify` list used by **Set null**. |
| `utilities.data.normalize_update_fields` | `models/projects.py` | Converts `update_fields` to a frozenset in the forwarded save arguments. Membership checks then cannot exhaust a one-shot iterable before the save. NetBox uses the same pattern. |
| `utilities.rqworker.get_queue_for_model` | `api/views.py`, `jobs.py` | Selects the configured queue for both worker checks and enqueue operations. |
| `rq.utils.parse_timeout` | `runtime/introspection.py` | Parses the author's `job_timeout` using RQ's own grammar, including strings such as `"30m"`. |
| `rq.exceptions.TimeoutFormatError` | `runtime/introspection.py` | Converts an invalid RQ timeout string into a Script metadata error. |
| `rq.timeouts.JobTimeoutException` | `jobs.py` | Propagates the worker deadline rather than classifying it as a content error. Timed-out work releases its lease without a permanent verdict. |
| `utilities.rqworker.any_workers_for_queue` | `api/views.py` | Checks whether the selected queue has a worker before accepting a REST run. |
| `utilities.rqworker.get_all_workers` | `migration/cutover.py` | Lists registered workers by name. Cutover requires acknowledgement when more than one is registered. This check does not prevent later worker startup or scheduler activity. |
| `drf_spectacular.drainage.GENERATOR_STATS` | `tests/api/test_schema.py` | Suppresses whole-API schema warnings while tests inspect the plugin's operations. NetBox's tests use the same import. |
| `utilities.exceptions.RQWorkerNotRunningException` | `api/views.py` | Returns 503 when no worker is available, matching NetBox's run endpoint. |
| `netbox.api.authentication.TokenPermissions` | `api/views.py` | Provides the base permission class for the REST run action, where POST requires `run` rather than `add`. |
| `extras.models.ScriptModule` and its manager | `migration/source.py` | Reads built-in modules through a `ManagedFile` proxy whose manager includes both `scripts` and `reports` roots. |
| `extras.models.Script` through `module.scripts` and its manager | `migration/source.py` | Reads the classes published by built-in modules and distinguishes Report-owned classes by the module file root. |
| `core.models.ManagedFile` fields `file_root`, `file_path`, `data_path`, `data_source` | `migration/source.py` | Locates stored source and its Data Source path. NetBox marks `ManagedFile` as `_netbox_private`. |
| `core.choices.ManagedFileRootPathChoices.SCRIPTS` and `.REPORTS` | `migration/source.py` | Separates Custom Scripts from Reports. Source reads select `SCRIPTS`, reference reads exclude Report-owned targets, and inventory counts excluded Report modules. |
| `extras.models.mixins.PythonModuleMixin.python_name` | `migration/source.py` | Provides a label: the filename stem, or the parent directory for `__init__.py`. It is not an importable dotted path. |
| `storages['scripts']` | `migration/source.py` | Opens built-in module source by `file_path`, matching the built-in loader. |
| `extras.models.EventRule.action_object_type` | `migration/source.py` | Counts Event Rules whose action needs repointing. |
| `users.models.ObjectPermission.object_types` | `migration/source.py` | Counts permissions referring to the built-in models. |
| `core.models.Job.object_type` | `migration/source.py` | Counts associated Job history, including scheduled and recurring work. |
| `extras.models.ScriptModule.jobs` and `.event_rules` | `migration/source.py` | Identifies history and Event Rules that module deletion would also remove. Both are `GenericRelation`s, so Django's deletion collector cascades to those rows. |
| `users.models.ObjectPermission.enabled` | `migration/cutover.py` | Disables captured Object Permission grants on the built-in feature. This is not an installation-wide write fence. |
| `extras.models.EventRule.enabled` | `migration/cutover.py`, `migration/references.py` | Disables captured rules during handover and restores eligible rules after repointing their action and source types. |
| `core.models.Job.terminate` | `migration/cutover.py` | Mirrored rather than called. Cancellation fails a waiting Job with a conditional update, because `terminate()` saves the whole row and would overwrite a start by a worker that took the task first. The Notification `terminate()` sends the owner of a failed Job is created the same way instead. |
| `django_rq.get_queue` and `rq.job.Job.fetch` / `.delete` / `.exists` | `migration/cutover.py`, `migration/references.py` | Captures input from the RQ task, since `Job.enqueue()` does not store it on the Job row, then deletes the task. The scheduler can re-enqueue a due task fetched before deletion. Replay checks whether the task exists again, which a claim or a scheduler's re-queue writes back. |
| `rq.exceptions.NoSuchJobError` | `migration/cutover.py` | Handles a missing RQ task. Capture marks its input unrecoverable, and cancellation treats the task as already deleted. |
| `netaddr.IPAddress` and `.IPNetwork` | `migration/cutover.py` | The values an IP address or network variable cleans to, recorded as text the replacement's form field parses back. `netaddr` arrives with NetBox, which the plugin relies on without declaring. |
| `core.models.AutoSyncRecord` | `migration/cutover.py` | Removes built-in synchronization registrations. These use the **concrete** `ManagedFile` content type, unlike Job and Event Rule references to the proxy. |
| `extras.models.EventRule.action_type`, `.action_object_type`, `.action_object_id`, `.object_types` | `migration/references.py` | Repoints actions and source types to replacement Scripts. Calls `full_clean()` first because a rule may already be invalid. |
| `users.models.ObjectPermission` creation, `.actions`, `.object_types`, `.users`, `.groups` | `migration/references.py` | Maps grants to plugin models and splits grants that also cover unrelated types. |
| `users.models.Group` | `migration/references.py` | Resolves captured groups before assigning them to the replacement permission. Groups deleted since capture are reported rather than written as raw keys. |
| `core.models.Job.object_type` / `.object_id` update | `migration/references.py` | Repoints Script Job history in batches before deletion, which would otherwise cascade to those Jobs. |
| `extras.models.ScriptModule.delete` | `migration/cleanup.py` | Deletes eligible modules and their stored source per instance. `QuerySet.delete()` would skip the model's `delete()` method that removes the file. |

Dependencies are grouped by responsibility. Execution integration lives in
`execution.py`. API dependencies cover run dispatch, declaration authorization
and write-lock ordering. The permission helper uses the same refusal exception
as NetBox's object-edit views.

**Keep the table and canary in sync.** Each row has a probe in
`scripts/check_netbox_internals.py`, in the same order. A test checks that they
match. The canary resolves symbols after `django.setup()` without querying the
database. It also fails if NetBox skips loading the plugin because the host is
outside its supported version range.

CI runs the canary against NetBox's `feature` branch as a blocking check. The
full test leg on that branch is advisory. **The canary checks symbol presence,
not signatures, relation types or behavior.** Those contracts need the full
suite against the pinned NetBox ref.

Run it locally from the plugin repository root:

```bash
PYTHONPATH=$PWD/testing NETBOX_CONFIGURATION=configuration \
  python scripts/check_netbox_internals.py --netbox /path/to/netbox/netbox
```

**Migration lookups.** `migration/source.py` owns discovery of built-in module
and Script rows. The write passes act on those results, keeping knowledge of the
legacy source layout in one place.

Keep these path and content-type distinctions intact:

- Open stored source with `file_path`, not `full_path`. The latter adds a
  filesystem root, while the built-in loader uses the relative storage key.
- Resolve built-in Script and ScriptModule content types with
  `for_concrete_model=False`. Jobs and Event Rules record the proxy types.
  Synchronization registrations use the concrete `ManagedFile` type instead.

**Bulk import.** The source-field gate in `views/projects.py` checks the instance
NetBox is about to save, not the form's `cleaned_data`. It relies on two
undocumented behaviors: a blank CSV cell preserves the stored value, and NetBox
removes fields absent from the record before constructing the instance. A change
to either could cause unchanged imports to require `activate`, refusing updates
rather than bypassing authorization.

## Why they are not avoidable

These dependencies are not optional. Without the request processors, a Script's
changes lose their user attribution and its events never reach webhooks or
Event Rules. Without the router probe, a Script running inside a branch writes
through a connection the run's transaction does not cover, so a dry run may not
revert and a failure may not roll back. Without a safe request copy, the run
cannot reach a worker.

The plugin does not depend on NetBox's `AbortTransaction`. It needs only an
exception to trigger dry-run rollback, so it defines its own equivalent.

## The advisory-lock namespaces

The plugin also depends on two PostgreSQL advisory-lock namespaces, which are
not symbols in the table:

- `(770100, .)` for Projects, keyed by `storage_key` in `storage/locks.py`.
- `(770101, 1)` for the migration run in `models/migration.py`.

The migration namespace is derived from the Project namespace. Changing the
first changes both.

Session-level locks use `django_pg_utils.advisory_lock` from `django-pgware`,
which NetBox supplies through its pinned requirements and uses in
`netbox/jobs.py`, `extras/jobs.py` and `ipam/api/views.py`. Database-only Project
and declaration writes use `project_write_lock()` on the same Project key via
`pg_advisory_xact_lock`. That lock lasts until the enclosing transaction ends.

**Separation from NetBox's locks depends on the numeric keys, not their arity.**
PostgreSQL separates one-bigint keys from two-integer keys, but NetBox uses both.
For example, `CustomField.data_lock_key()` returns
`(ADVISORY_LOCK_KEYS['custom-field-data'], pk)`. NetBox takes that pair at session
scope in `extras/jobs.py` and at transaction scope in
`extras/models/customfields.py`.

NetBox's keys are registered in `ADVISORY_LOCK_KEYS` in `netbox/constants.py` or
hashed per tree by `utilities/ltree.py` triggers. The registered range recorded
here is 100100 to 115100, separate from 770100 and 770101. The canary checks every
registered key against both plugin namespaces and fails the `feature`-branch
check on a collision. Check the live constants when upgrading.

Like `django_rq` and `strawberry`, the advisory-lock helper is supplied by NetBox
rather than declared separately by this plugin.

## The ask upstream

**A documented execution context.** The upstream request is for an API that
owns transactions on the default and routed aliases, applies request processors
according to commit intent, defines processor startup failures, rolls back dry
runs, clears pending events on failure and restores request context on every
exit. It would replace the execution dependencies above except `AbortScript`.
These requirements also apply to other plugins executing user-supplied code.

**Legacy abort compatibility.** `AbortScript` belongs to the built-in Custom
Scripts feature and is expected to leave with it at v5.0. Until then, unchanged
legacy Scripts can raise it. No separate import guard is used: on a host above
`max_version`, NetBox reads the plugin configuration and skips loading it. Only
`__init__.py` and `constants.py` load on that path, and neither imports the
legacy exception.

**Attribution without a synthetic request.** The context should accept an
attribution identity without requiring a synthetic HTTP request. The dependency
described here is `core.signals.handle_changed_object`: it reads
`current_request` and returns without recording an ObjectChange or queuing that
change's event when no request is set. This concerns collecting and attributing
changes, not flushing events already queued by a committed run. A supported
context should make both responsibilities explicit.

**Composable request processors.** `utilities.request.apply_request_processors()`
is close to the loop in `execution.py`, but cannot express two requirements:
skipping `event_tracking` for dry runs and treating its startup failure as fatal.
Parameters for those choices, or a documented composition interface, would
remove the local loop.

**Job data before execution.** `Job.enqueue()` has no argument for setting the
new Job row's `data`. With `immediate=True`, it also invokes the handler before
returning. The plugin therefore creates the immediate Job itself so revision
provenance exists before execution. A row-data argument or a documented
post-save, pre-handler hook would remove that duplication.

**A legacy export service.** Migration needs serializable module and class
records, content checksums, Data Source references, repository paths, source
streams and the Event Rules, permissions, Job history, schedules and recurrences
that refer to them. A supported service would replace the reads in
`migration/source.py` and reduce dependence on field names in a feature being
retired.

The write dependencies would remain. Migration validates, orders and records
changes that operators could also make through NetBox. Those writes still need
compatibility checks as the built-in models change.

**A write and execution fence.** This is a separate request for persistent,
NetBox-owned state enforced below the views. It should make the built-in UI,
REST API, Event Rule dispatch and `runscript` refuse new work through one shared
check.

A plugin can refuse ordinary row saves with a `pre_save` receiver that raises
`AbortRequest`, including without a request in scope. NetBox documents the
request-abort pattern, and `handle_deleted_object` also raises it for a
protection rule before checking for a request. Direct saves send the proxy as
sender, while automatic synchronization saves the concrete `ManagedFile`. A
receiver intended to cover both must connect to both senders.

That does not provide a complete fence. A row-save signal does not prevent
execution, and synchronization has two additional limits:

- **Storage writes happen separately.** `ManagedFile.sync_data()` writes through
  the storage backend without calling `save()`. `SyncedDataMixin.clean()` calls
  `sync()`, so validation can replace stored content before a save is refused.
- **Synchronization errors are not isolated per record.** The `auto_sync`
  receiver on `post_sync` loops over `AutoSyncRecord`s without per-record error
  handling. An exception stops later records, including config templates,
  export templates, config contexts and config context profiles. The sync has
  already recorded `COMPLETED`, but the Job changes it to failed. Raising to
  refuse one record can therefore disrupt unrelated synchronization. Per-record
  isolation would address this for other plugins too.

**Current migration limits.** The plugin does not provide this complete fence.
Cutover disables captured Object Permissions and Event Rules, cancels reachable
waiting tasks and removes built-in synchronization registrations. Cleanup deletes
eligible mapped modules and their source, retaining modules with protected
history or references. It does not delete every built-in row. See
[cleanup](../migration.md#retiring-the-built-in-rows) for retained and blocked
modules.

Permission withdrawal does not restrict superusers, permissions supplied through
`DEFAULT_PERMISSIONS`, or plain Django grants honored by `RemoteUserBackend`
through `ModelBackend`. A privileged user can also grant
`extras.add_scriptmodule` again and upload new built-in Custom Scripts.

Task deletion and failed Job status do not guarantee that a cancelled run
cannot execute. The scheduler can enqueue a task fetched before deletion, even
with one worker. The reference pass then holds back its replacement, because
the run has started or its task is queued again. The migration journal does not
provide exactly-once execution. Keep the
[worker and scheduler precautions](../migration.md#worker-arrangement) in the
migration guide as the operational reference.

Inventory and staging are repeatable and content-addressed, but that is not an
execution fence. Cutover still requires a maintenance window and control of new
submissions. A supported fence must address both writes and execution before the
plugin can promise that the built-in feature is closed.
