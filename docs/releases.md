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
