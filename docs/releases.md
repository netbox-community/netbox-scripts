# Change Log

## v0.0.1

* Initial release
* Requires NetBox 4.7.0 or later. The Event Rule action this plugin registers
  needs the plugin action registry that arrived in 4.7, so the floor is the
  version where every shipped feature works rather than the oldest that loads
* `ScriptProject` model with full UI, REST API, GraphQL, and
  global-search surfaces
* Custom Script authoring API: `Script` base classes, variable types, dynamic
  form generation, structured logging, and `AbortScript`
* Project storage: configurable storage roots and size limits, source-path
  safety checks, canonical manifests, and content digests
* `ScriptProjectRevision` model with immutable content fields, a status
  lifecycle, and content-addressed deduplication of identical source trees
* Revision staging and activation, with one active revision per project and
  storage reclaimed when a project or revision is deleted
* `ScriptFile` model declaring project entrypoints, with enabled
  declarations frozen into each revision as its entrypoint snapshot and the
  snapshot digest joining the revision identity
* `ScriptFile` UI, REST API, GraphQL, and global-search surfaces, with
  the discovery fields readable and filterable but writable only by project
  validation, and each project's modules listed on its detail page
* Entrypoint selection as a Project setting: an Entrypoints tab and a
  `projects/<id>/entrypoints/` REST operation list the importable modules of a
  project's source, at any depth, so a path is chosen rather than typed.
  Selection is expressed as `enabled`, so deselecting keeps a declaration's
  discovery history and its reserved path
* A Script File's project and source path are frozen after creation, so
  a declaration can never be repointed at a file it did not name
* Manifest-verified runtime cache: revisions materialize to disposable local
  trees that are verified before every use, purged of compiled files, staged
  whole, published read-only, and rebuilt from the store when damaged
* Private package loader: revision code imports under a generated namespace
  with real package semantics, isolated per project and revision, never
  shadowing installed distributions, with failed imports swept cleanly
* Custom Script discovery publishing entrypoint-defined classes, with
  `script_order` for ordering and explicit re-export, and project-qualified
  identity and logger markers on every published class
* Leased revision validation service and background job driving revisions to
  `valid` or `invalid` verdicts, with owner-fenced transitions, sanitized
  stored errors, and per-module discovery results
* Script upload: a Script Project can be created from one uploaded `.py`
  file, which is declared as an entrypoint, staged as a revision, validated in a
  worker, and activated when the Project's activation policy allows. Adding
  another script stages a revision holding the existing tree plus the new file,
  and replacing a path the Project already holds needs explicit confirmation
* Manual activation: a Project whose activation policy is manual can be put into
  service from its own page, naming the revision that would go live and retiring
  the previous one in the same step
* Project-scoped serialization for the storage lifecycle: staging, entrypoint
  refresh, activation, and physical cleanup hold one advisory lock keyed by the
  Project's immutable storage key, and cleanup rechecks for a referencing
  revision under that lock before reclaiming content
* Source state on the Project page: a plain-language summary plus the current
  revision's date, status, digest, file count, size, and activation time, with
  the full revision history on its own Revisions tab
* Repair Scripts on the Project page: republishes a serving Project's Custom
  Script rows from the revision it already serves, reporting how many moved,
  behind the same activate permission as activation itself
* A read-only Files tab on the Project: the current revision's files with
  size, short checksum, and entrypoint state, annotating a declared path the
  source no longer holds
* Fixed: the runtime cache created its intermediate directories at the process
  umask while applying its private mode to the leaf only, so its own privacy
  check rejected the default cache root on any host with a group-writable umask
* Fixed: activation compared a revision's entrypoint digest but not the snapshot
  itself, so a snapshot swapped after its return-trip check could be activated
* `CustomScript` model recording one published Script class per row, parented on
  the Project because `script_order` lets a helper module publish a class.
  Activating a revision synchronizes the rows in the same transaction that moves
  the pointer, and a class the active revision stops publishing is retired
  rather than deleted, so its Job history survives
* `CustomScript` UI, REST API, GraphQL, and global-search surfaces. Rows are
  derived rather than authored, so the surface offers list, detail, edit, and
  bulk edit, and refuses create, delete, and bulk import on every one of them
* An administrator can set a Custom Script's `enabled` from its edit form, in
  bulk from the list view, or with a REST PATCH. The field was documented as the
  administrator's from the start but no surface could write it, so it was
  effectively always true. Synchronization still never touches it
* A Project's detail page lists the Custom Scripts it has published, retired
  ones included, filtered to that Project
* Fixed: a Project holding an active revision could not be deleted through any
  user-facing path, because the active-revision reference protected the Project
  from itself and cascading revisions had no serializer for their delete events
* Fixed: re-uploading source a Project has held before resolved to the existing
  content-addressed revision, which already carried a verdict, so validation was
  enqueued against a terminal revision and left a failed job
* Custom Script execution: a published script is run from its own page, against
  the revision its Project was serving when the run was requested, so a queued
  run executes the source the operator was looking at even after the Project has
  moved on. The form is built from the class's own variables and fieldsets, and
  the run is recorded as a Job carrying its log, its output, and the revision it
  used
* Commit and dry run preserved: with commit on, changes are written and change
  logging, webhooks and Event Rules behave as they do for any request, and with
  commit off everything is rolled back while the log and output are still
  recorded and no events are queued
* Running is its own permission, `run`, granted separately from `change`, and it
  is rechecked when a worker starts, so disabling a script stops a run that was
  already queued
* Each run imports its revision fresh and unloads it afterwards, so module-level
  state never carries from one run into the next, and the run record is stripped
  of storage keys, digests and cache paths, including inside a traceback
* Both this plugin's `AbortScript` and the one a script written for NetBox's
  built-in runner raises end a run cleanly, which matters while the two
  implementations coexist
* A Custom Script is executable only while its Project is serving a revision.
  That was previously implied by retirement rather than checked
* Data Source reconciliation: every completed synchronization of a Data Source
  rebuilds the source of each Project on it from the whole directory as it
  stands, stages that as a revision, validates it, and activates it when the
  Project's activation policy allows. Reconciliation is one background Job per
  Project, so a Project's own failure never fails the operator's synchronization
  or affects its siblings, and a synchronization that changed nothing produces no
  revision at all
* A Python file appearing in a synchronized directory is a candidate rather than
  an entrypoint, so a repository cannot publish a Custom Script by itself and an
  administrator's entrypoint selection survives every synchronization. A
  selected entrypoint that disappears from the source makes the new revision
  `invalid` naming the path, while the Project keeps serving the revision it
  already had
* Compiled Python files and `__pycache__` directories are skipped wherever they
  sit in a synchronized directory. Every other refused path is recorded on an
  invalid revision rather than dropped, and every file is stored rather than only
  Python modules, because a script reads templates and data next to it
* Reconcile Source: a Data Source-backed Project can be rebuilt from the current
  file inventory on demand, which covers a Project created between
  synchronizations and an entrypoint selection that should take effect now. It
  deliberately does not synchronize the Data Source itself
* Returning a synchronized directory to a tree the Project has held before
  resolves to the revision that already validated it, and an automatically
  activated Project puts that revision back into service. Without this a revert
  in the source would silently change nothing
* Fixed: a Data Source-backed Project offered the Add Script button and answered
  the upload with a server error, because ingestion refuses an upload into a
  Project whose source is synchronized and the refusal surfaced out of the form's
  save rather than its validation
* Fixed: changing the entrypoint selection reported a change it never applied. A
  revision freezes the Project's enabled declarations when it is staged, so a new
  selection took effect only at the next ingestion, and an uploaded Project had
  no route to one at all. Saving the Entrypoints tab, or the REST operation
  behind it, now restages the stored source under the new selection and drives it
  to a verdict, and does so only when the selection actually moved
* Scheduled and recurring runs: a run can be deferred to a time in the future or
  set to repeat, and a Custom Script whose author disabled scheduling offers
  neither field. A one-shot run stays pinned to the revision it was requested
  against, while a recurrence resolves the active revision at each occurrence,
  because a pinned recurrence would execute one frozen revision indefinitely
  however often the Project was updated since
* A Data Source-backed Project may sit at the root of its Data Source, meaning an
  empty data path, which takes every file in the source. An empty path overlaps
  every other path on the same Data Source, so two Projects still cannot claim
  the same files
* Revisions are readable over REST and GraphQL, filtered by Project, status, or
  either digest, and each one has a detail page of its own. The manifest, the
  entrypoint snapshot, and the validation lease stay off both surfaces, being
  internal to the storage and validation services rather than user-facing state
* A Custom Script can be run over REST, with `POST scripts/<id>/run/`. The
  request body is the shape NetBox's built-in script endpoint already accepts, so
  a caller moving over changes the URL and nothing else, and the reply is the Job
  that was queued. Variable values nest under `data`, which keeps a variable
  named `commit` or `interval` from colliding with an execution parameter, and
  they are validated by the same form the run page renders. A run is refused when
  no worker is running rather than queued where nothing would pick it up
* Managing what code a Project runs is separate from editing the Project.
  Activating a revision and reconciling a source each take their own permission,
  `activate` and `reconcile`, rather than borrowing `change`, so someone who may
  rename a Project cannot thereby choose the code it serves. Scheduling is a
  third new permission on the Custom Script, `schedule`, which composes with the
  author's own setting: the run form withholds the two scheduling fields unless
  both allow them, and REST refuses the values because it has no form to leave
  them out of. Changing entrypoints and reading run results deliberately get no
  new codename, because the Script File `change` permission and NetBox's
  own Job permission already name those privileges exactly
* A script written for NetBox's built-in runner works unmodified. Its
  `extras.scripts` import resolves to this plugin's authoring API in every form
  the language allows, including the dotted and package forms and an import
  deferred into a function body, while an import of anything else under `extras`
  still reaches NetBox. Stored source is never rewritten, NetBox's own module is
  never replaced, and built-in scripts keep running alongside. A dynamic
  `importlib.import_module('extras.scripts')` is the one form out of reach. The
  layer is transitional: it serves while NetBox ships that module and then fails
  with a message naming the migration, so it has an end rather than becoming
  permanent
* A legacy Report is refused rather than emulated. A class declaring `test_*`
  methods and no `run()` makes the revision `invalid` naming the class, instead
  of publishing something that looks runnable and raises once an operator presses
  Run. Only a callable counts, so a script with an attribute named `test_mode`
  still publishes
* Fixed: an import error naming a member that does not exist was classified as an
  environment fault, so an author's typo failed the validation job with no
  verdict recorded rather than producing an invalid revision naming the line.
  Only an absent module is treated as the environment's now
* A migration inventory pass reports what moving off NetBox's built-in Custom
  Scripts would do, and changes nothing. It classifies every built-in module's
  authoring dialect by parsing the stored source rather than importing it, names
  the Projects a migration would create, and counts the Event Rules, permissions
  and Jobs a cutover would have to repoint. Modules on the legacy `extras.scripts`
  import are reported apart from report-style ones, because the first needs one
  line changed before NetBox v5.0 and the second needs a rewrite at any version,
  so the first list is a work queue with a deadline
* A migration staging pass creates those Projects and stages their content, leaving
  the built-in feature authoritative and activating nothing. A Project is created
  per folder that holds scripts, so a migrated Project holds the helper modules
  beside a script, which the built-in feature could never synchronize. Staging is
  re-runnable because identity comes from the source rather than from bookkeeping,
  and it refuses outright while any finding blocks, so a file name nothing could
  import is fixed before content is written. Both passes are jobs rather than
  management commands, and neither has an interface yet
