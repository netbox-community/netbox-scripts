# Change Log

## v0.1.0 (2026-11-12)

!!! warning "Beta release"

    NetBox Scripts v0.1.0 is a beta for testing and is not recommended for
    production use. Behavior, defaults and APIs can change before a stable
    release. It requires NetBox v4.7.5 or a later v4.7 release. Read the
    Upgrading from v0.0.1 section below before upgrading. Known bugs are
    tracked as
    [open bug reports](https://github.com/netbox-community/netbox-scripts/issues?q=is%3Aissue+is%3Aopen+label%3A%22type%3A+bug%22).

### Enhancements

* [#38](https://github.com/netbox-community/netbox-scripts/issues/38) - Add worked examples of event-driven Scripts to the Event Rules guide
* [#39](https://github.com/netbox-community/netbox-scripts/issues/39) - Add examples of object-selection fields to the authoring guide
* [#40](https://github.com/netbox-community/netbox-scripts/issues/40) - Document caching an external lookup between Script runs
* [#137](https://github.com/netbox-community/netbox-scripts/issues/137) - Document that Project revisions do not preserve symbolic links or device files

### Bug Fixes

* [#1](https://github.com/netbox-community/netbox-scripts/issues/1) - Recreate the schedule of a built-in run cancelled by migration cutover when its stale task reappears, since NetBox v4.7.5 no longer runs it
* [#2](https://github.com/netbox-community/netbox-scripts/issues/2) - Keep migration cutover open while built-in Custom Script Jobs are waiting or running, so with NetBox v4.7.5 a recurring successor queued as it completes is not missed
* [#3](https://github.com/netbox-community/netbox-scripts/issues/3) - Prevent a cancelled run from ending as Errored after committing its changes, fixed by NetBox v4.7.5
* [#4](https://github.com/netbox-community/netbox-scripts/issues/4) - Report waiting or running built-in runs as blocking in migration verification rather than as historical Jobs
* [#5](https://github.com/netbox-community/netbox-scripts/issues/5) - Prevent Repair Scripts from reactivating an older revision during a concurrent activation or deactivation
* [#6](https://github.com/netbox-community/netbox-scripts/issues/6) - Read each Data Source directory in one query, so with NetBox v4.7.5 each reconciled revision holds one complete synchronization
* [#7](https://github.com/netbox-community/netbox-scripts/issues/7) - Give each occurrence of a recurring run its own copy of its inputs
* [#8](https://github.com/netbox-community/netbox-scripts/issues/8) - Restage the Project when a Script File is enabled or disabled on its edit page or through REST
* [#9](https://github.com/netbox-community/netbox-scripts/issues/9) - Prevent deadlocks between activation and Script bulk edits
* [#10](https://github.com/netbox-community/netbox-scripts/issues/10) - Fix recovery from a stale Running Job that blocks migration cutover after its worker stops
* [#11](https://github.com/netbox-community/netbox-scripts/issues/11) - Name the Running Job that holds back an interrupted migration pass, on the Migration page and in its refusal
* [#12](https://github.com/netbox-community/netbox-scripts/issues/12) - Fix an unhandled error when a waiting Job is deleted during migration cutover
* [#13](https://github.com/netbox-community/netbox-scripts/issues/13) - Accept and validate a reused revision left in Staging or Storage failed once its source verifies as stored
* [#14](https://github.com/netbox-community/netbox-scripts/issues/14) - Stop cleanup from asking to repeat the completed reference step, and name the manual step that releases a module
* [#15](https://github.com/netbox-community/netbox-scripts/issues/15) - Send the notification for a Job cancelled by an interrupted cutover once the pass resumes
* [#16](https://github.com/netbox-community/netbox-scripts/issues/16) - Describe FileVar multipart uploads in the REST run action's schema
* [#17](https://github.com/netbox-community/netbox-scripts/issues/17) - Stop reporting exceptions from a Script's own constructor or run form as source-loading failures
* [#18](https://github.com/netbox-community/netbox-scripts/issues/18) - Report a runtime cache root that cannot be inspected with the plugin's error rather than a raw permission error
* [#19](https://github.com/netbox-community/netbox-scripts/issues/19) - Identify each affected schedule by its Job ID in migration verification
* [#20](https://github.com/netbox-community/netbox-scripts/issues/20) - Report in Job logs when a validation is queued again after its lease expired
* [#21](https://github.com/netbox-community/netbox-scripts/issues/21) - Keep the synchronization registration count when a cutover pass resumes
* [#22](https://github.com/netbox-community/netbox-scripts/issues/22) - Prevent a directory named "(root)" from sharing the top-level heading on the Script Files tab
* [#62](https://github.com/netbox-community/netbox-scripts/issues/62) - Report failed validation in reconciliation logs rather than as a storage failure
* [#64](https://github.com/netbox-community/netbox-scripts/issues/64) - Fix cutover failing on waiting built-in Jobs with an empty queue name
* [#65](https://github.com/netbox-community/netbox-scripts/issues/65) - Keep the migration open while a retained module still has an unfinished built-in run
* [#66](https://github.com/netbox-community/netbox-scripts/issues/66) - Prevent REST updates from deadlocking with activation or uploads on NetBox v4.7.4 and later
* [#67](https://github.com/netbox-community/netbox-scripts/issues/67) - Report the active revision as the newest source after reusing earlier source, rather than a retired revision
* [#73](https://github.com/netbox-community/netbox-scripts/issues/73) - Allow FileVar uploads to share names with REST run parameters
* [#74](https://github.com/netbox-community/netbox-scripts/issues/74) - Apply the Script's commit default to multipart REST runs that omit `commit`
* [#75](https://github.com/netbox-community/netbox-scripts/issues/75) - Report network and file errors raised by a running Script as execution failures rather than source access failures
* [#76](https://github.com/netbox-community/netbox-scripts/issues/76) - Advertise only the media types the upload and Script File selection actions accept in the REST schema
* [#77](https://github.com/netbox-community/netbox-scripts/issues/77) - Prevent failed REST uploads from exposing internal error details
* [#79](https://github.com/netbox-community/netbox-scripts/issues/79) - Stop the cleanup confirmation from treating retained Job history as a blocker
* [#82](https://github.com/netbox-community/netbox-scripts/issues/82) - Restrict object variables on the run form and REST run action to objects the user can view
* [#83](https://github.com/netbox-community/netbox-scripts/issues/83) - Preserve every value of a repeated query parameter in Script run links
* [#85](https://github.com/netbox-community/netbox-scripts/issues/85) - Restore the object selector and Quick add dialogs on the Script Run page
* [#88](https://github.com/netbox-community/netbox-scripts/issues/88) - Prevent the Script Run page from showing storage backend error text
* [#96](https://github.com/netbox-community/netbox-scripts/issues/96) - Reject runtime cache paths reached through untrusted symbolic links
* [#99](https://github.com/netbox-community/netbox-scripts/issues/99) - Prevent migration from recreating a recurring run whose deleted Job returns
* [#100](https://github.com/netbox-community/netbox-scripts/issues/100) - Repoint only finished Job history, so unfinished built-in runs stay visible to migration checks
* [#112](https://github.com/netbox-community/netbox-scripts/issues/112) - Prevent Project bulk edits from overwriting changes made while waiting for a lock
* [#114](https://github.com/netbox-community/netbox-scripts/issues/114) - Enforce permissions and ownership of runtime cache Project directories
* [#118](https://github.com/netbox-community/netbox-scripts/issues/118) - Prevent failed activation, repair and deactivation from showing internal storage error details
* [#119](https://github.com/netbox-community/netbox-scripts/issues/119) - Prevent revision validation failures from exposing storage backend paths
* [#120](https://github.com/netbox-community/netbox-scripts/issues/120) - Identify permissions by ID in migration warnings and verification
* [#121](https://github.com/netbox-community/netbox-scripts/issues/121) - Report a run's changes as committed and queue its events when a post-commit callback fails
* [#122](https://github.com/netbox-community/netbox-scripts/issues/122) - Report a run whose processing fails after its changes commit as committed rather than unfinished
* [#131](https://github.com/netbox-community/netbox-scripts/issues/131) - Prevent migration activation outcomes from including internal storage error details
* [#132](https://github.com/netbox-community/netbox-scripts/issues/132) - Stop marking a removed source path as awaiting activation on the Revision Files tab
* [#133](https://github.com/netbox-community/netbox-scripts/issues/133) - Prevent readable runtime cache lock files from blocking Script loading
* [#134](https://github.com/netbox-community/netbox-scripts/issues/134) - Identify Scripts by Project and module in migration verification
* [#138](https://github.com/netbox-community/netbox-scripts/issues/138) - Prevent revision imports from running bytecode outside the verified cache tree when `PYTHONPYCACHEPREFIX` is set
* [#140](https://github.com/netbox-community/netbox-scripts/issues/140) - Keep storage backend paths out of reconciliation, Script File refresh and Script run Job logs
* [#148](https://github.com/netbox-community/netbox-scripts/issues/148) - Prevent concurrent Script imports from failing with "dictionary changed size during iteration"
* [#150](https://github.com/netbox-community/netbox-scripts/issues/150) - Keep storage keys and full revision digests out of errors for missing or damaged stored revisions
* [#152](https://github.com/netbox-community/netbox-scripts/issues/152) - Deliver Event Rule actions for changes committed by `runcustomscript` runs
* [#161](https://github.com/netbox-community/netbox-scripts/issues/161) - Limit the object inputs of schedules recreated by migration to objects their owners can view

### Other Changes

* [#45](https://github.com/netbox-community/netbox-scripts/issues/45) - Isolate Redis-dependent tests when the suite runs in parallel
* [#49](https://github.com/netbox-community/netbox-scripts/issues/49) - Align UI test sessions across the NetBox version matrix
* [#58](https://github.com/netbox-community/netbox-scripts/issues/58) - Fix the stale-copy Script File API test on NetBox v4.7.4
* [#68](https://github.com/netbox-community/netbox-scripts/issues/68) - Correct the release checklist's tagging and publishing steps
* [#123](https://github.com/netbox-community/netbox-scripts/issues/123) - Raise the minimum NetBox version to v4.7.5

### Upgrading from v0.0.1

Installations of v0.0.1 upgrade in place, which replaces the v0.0.1 notes'
statement that the beta has no upgrade path. The database schema is unchanged,
so `migrate` has nothing to apply, and stored source, configuration and the
runtime cache layout are unchanged too. Back up the database, configuration and
source storage before upgrading.

- **Upgrade NetBox to v4.7.5 or a later v4.7 release first** ([#123](https://github.com/netbox-community/netbox-scripts/issues/123)).
  NetBox v4.7.4 and earlier refuse to load this release.
- **Runtime cache** ([#96](https://github.com/netbox-community/netbox-scripts/issues/96), [#114](https://github.com/netbox-community/netbox-scripts/issues/114), [#133](https://github.com/netbox-community/netbox-scripts/issues/133)). A Project directory that other
  users can write, for example one created under a group-writable umask, is
  now refused, and so is a directory or symbolic link on the cache path that
  belongs to an account other than NetBox's or root. Lock files become private
  on their next use, and one this process cannot open or make private stops
  its revision from loading. If other local accounts can traverse the cache,
  stop NetBox's processes, remove the existing `.<digest>.lock` files and
  start them again, since a changed mode cannot revoke a descriptor already
  open. See [Runtime cache](administration/configuration.md#runtime-cache) for
  each remedy.
- **`PYTHONPYCACHEPREFIX`** ([#138](https://github.com/netbox-community/netbox-scripts/issues/138)). If it was set, remove its mirror of
  `runtime_cache_root`, which holds compiled copies of revision code.
- **Recurring schedules** ([#7](https://github.com/netbox-community/netbox-scripts/issues/7)). A recurring run queued before the upgrade
  keeps any changes its earlier occurrences made to its inputs. Recreate such
  schedules.
- **A migration in progress** ([#2](https://github.com/netbox-community/netbox-scripts/issues/2), [#65](https://github.com/netbox-community/netbox-scripts/issues/65), [#100](https://github.com/netbox-community/netbox-scripts/issues/100), [#161](https://github.com/netbox-community/netbox-scripts/issues/161)). Steps it has not
  completed resume under stricter checks, and completed steps are not
  reopened. While a built-in Job is waiting or running, an unfinished cutover
  or Job history step stays open and cleanup keeps its module, and Verify
  reports such a Job as blocking. An unfinished cutover retries a schedule
  whose queue task could not be read while that Job still waits, and one
  still missing its task keeps the step open, see
  [Missing queue tasks](administration/migration.md#missing-queue-tasks).
  A run v0.0.1 already cancelled because its input could not be read still
  needs to be recreated by hand. Repoint references skips, with a warning, a
  schedule with an object input its owner cannot view, rather than recreating
  it. A waiting or running Job that v0.0.1's Job history step already moved is
  outside these checks, so look for one on each migrated Script's Jobs tab
  before cleanup.
- **Text recorded by v0.0.1** ([#77](https://github.com/netbox-community/netbox-scripts/issues/77), [#119](https://github.com/netbox-community/netbox-scripts/issues/119), [#131](https://github.com/netbox-community/netbox-scripts/issues/131), [#140](https://github.com/netbox-community/netbox-scripts/issues/140), [#150](https://github.com/netbox-community/netbox-scripts/issues/150)). Job logs
  and Job data, revision failure reasons and migration journals can name
  storage backend paths, and for a missing or damaged revision its storage key
  and digest. NetBox removes old Jobs per `JOB_RETENTION`, never when it is 0.
  A revision's failure reason keeps the text until the revision is validated
  again. Migration journals keep it.
- **Changed behavior** ([#8](https://github.com/netbox-community/netbox-scripts/issues/8), [#74](https://github.com/netbox-community/netbox-scripts/issues/74), [#82](https://github.com/netbox-community/netbox-scripts/issues/82)). Enabling or disabling a Script
  File also needs the Project's change permission, and REST answers 403
  without it. A multipart REST run that omits `commit` follows the Script's
  default instead of running dry. Object fields on the run form and the REST
  run action accept only objects the user can view.

### Known Issues

* [#111](https://github.com/netbox-community/netbox-scripts/issues/111) - Concurrent bulk writes can deadlock when acquiring Project locks
* [#130](https://github.com/netbox-community/netbox-scripts/issues/130) - Bulk import overwrites Project changes made while waiting for a lock
* [#135](https://github.com/netbox-community/netbox-scripts/issues/135) - Source state reports source rejected before storage as failed validation
* [#136](https://github.com/netbox-community/netbox-scripts/issues/136) - Source state reports the active revision as newest while reused source waits for activation
* [#139](https://github.com/netbox-community/netbox-scripts/issues/139) - Invalid revision errors expose server installation paths
* [#146](https://github.com/netbox-community/netbox-scripts/issues/146) - Data Source revisions can import native extensions instead of reviewed Python source
* [#147](https://github.com/netbox-community/netbox-scripts/issues/147) - Job timeouts during stored-file reads are treated as storage failures
* [#151](https://github.com/netbox-community/netbox-scripts/issues/151) - Deleting a Project or revision while a Job runs causes an unnecessary failure
* [#153](https://github.com/netbox-community/netbox-scripts/issues/153) - Script result page omits Job-level failure reasons
* [#159](https://github.com/netbox-community/netbox-scripts/issues/159) - A rolled-back Script run is marked Completed and queues its events
* [#164](https://github.com/netbox-community/netbox-scripts/issues/164) - Migration activation Job log ends some outcomes with two periods
* [#165](https://github.com/netbox-community/netbox-scripts/issues/165) - Repoint references warnings expose runtime cache and storage paths
* With NetBox Branching earlier than v1.2.3, a Script run requested over REST logs "Running without the request processor ActiveBranchContextManager, it could not start". Upgrade NetBox Branching to v1.2.3 or later.
* The migration workflow has not yet been run on a live installation whose workers share queues with other work. Rehearse it on a copy first.

Compared with NetBox's built-in Custom Scripts, some functions are not available
yet. See [Limitations](features.md#limitations).

---

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
