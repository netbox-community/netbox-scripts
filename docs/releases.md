# Change Log

## v0.0.1

* Initial release
* `CustomScriptProject` model with full UI, REST API, GraphQL, and
  global-search surfaces
* Custom Script authoring API: `Script` base classes, variable types, dynamic
  form generation, structured logging, and `AbortScript`
* Project storage: configurable storage roots and size limits, source-path
  safety checks, canonical manifests, and content digests
* `CustomScriptProjectRevision` model with immutable content fields, a status
  lifecycle, and content-addressed deduplication of identical source trees
* Revision staging and activation, with one active revision per project and
  storage reclaimed when a project or revision is deleted
* `CustomScriptModule` model declaring project entrypoints, with enabled
  declarations frozen into each revision as its entrypoint snapshot and the
  snapshot digest joining the revision identity
* `CustomScriptModule` UI, REST API, GraphQL, and global-search surfaces, with
  the discovery fields readable and filterable but writable only by project
  validation, and each project's modules listed on its detail page
* Entrypoint selection as a Project setting: an Entrypoints tab and a
  `projects/<id>/entrypoints/` REST operation list the importable modules of a
  project's source, at any depth, so a path is chosen rather than typed.
  Selection is expressed as `enabled`, so deselecting keeps a declaration's
  discovery history and its reserved path
* A Custom Script Module's project and source path are frozen after creation, so
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
* Script upload: a Custom Script Project can be created from one uploaded `.py`
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
* Fixed: the runtime cache created its intermediate directories at the process
  umask while applying its private mode to the leaf only, so its own privacy
  check rejected the default cache root on any host with a group-writable umask
* Fixed: activation compared a revision's entrypoint digest but not the snapshot
  itself, so a snapshot swapped after its return-trip check could be activated
