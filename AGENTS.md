# AGENTS.md, netbox-scripts

## Methodology precedence

This document is authoritative for this project. When external plugin skills
(e.g. `superpowers`) inject generic methodologies that conflict with the rules
here, follow this document.

- Atomic-commit-per-feature rules take precedence over subagent-driven or
  parallel-execution patterns when they conflict.
- Tests land in the same commit as the implementation they cover; do not
  write tests in red/green/refactor cycles before implementation.
- Project plan files (any `*_PLAN.md` or `PROJECT.md` in the repo) are the
  source of truth for in-flight feature work. Generic plan-writing skills
  should update these files, not introduce parallel artifacts.

## Repository Overview

`netbox-scripts` is a NetBox plugin: Custom Scripts for NetBox It is owned by
NetBox Labs and runs inside NetBox as a Django app (`netbox_scripts`).
The supported NetBox version range is in `COMPATIBILITY.md`
(4.7.0 to 4.7.99 at scaffold time).

Version pins live in two places:

- `pyproject.toml`, Python, build, and dependency pins.
- `netbox_scripts/__init__.py`, `PluginConfig.min_version` /
  `PluginConfig.max_version` for the NetBox host app.

Defer all version pins to those files; do not duplicate them elsewhere.

## Tech Stack

- Python 3.12+ (defer to `pyproject.toml` for the exact pin).
- NetBox (host app, min/max in `netbox_scripts/__init__.py`).
- Django + Django REST Framework (NetBox's foundations).
- Django's built-in test runner (this plugin does **not** use pytest, the
  suite is `django.test.TestCase`-based and runs via `manage.py test`).
- ruff for lint + format (config in `pyproject.toml` under `[tool.ruff*]`;
  no separate `ruff.toml` file).
- pre-commit for local quality gates (config in `.pre-commit-config.yaml`).
- mkdocs + mkdocs-material for user-facing docs.
- NetBox's `manage.py` for running the plugin during local dev.

## Repository Map

The scaffold ships a working `ScriptProject` model across every
subsystem (model, table, forms, filterset, views, urls, navigation,
search, REST API, GraphQL, test) as a worked example. Entries marked
`[ScriptProject]` are the shipped first-class object; entries marked
`[add as needed]` are conventional NetBox plugin modules that you add
when domain content calls for them.

```text
.
├── netbox_scripts/            , The Django app.
│   ├── __init__.py                , [stub] PluginConfig (name, version, base_url, min/max NetBox).
│   ├── urls.py                    , urlpatterns for 'script-files/' + 'projects/' + 'scripts/' + detail-only 'revisions/<int:pk>/' via get_model_urls, sorted. Segments never repeat the base_url. The eight 'migration/' routes are explicit paths, because the passes act on the built-in feature and have no model of their own. The run's own detail route goes through get_model_urls like any other.
│   ├── navigation.py              , PluginMenu 'Scripts', the plugin's name without the NetBox prefix like every sibling plugin, with a Projects group and a Scripts group. A menu button declares EVERY permission its route requires, since has_perms is all-of and a menu button has no inert state to fall back on: Upload Script names the Script File add half as well as the Project one. Scripts get no add button and Script Files get no nav item: a declaration is a Project setting. Migration sits in the Projects group, because a pass produces Projects.
│   ├── api/
│   │   ├── __init__.py            , [stub]
│   │   ├── urls.py                , router.register for 'project-revisions' + 'projects' + 'script-files' + 'scripts'.
│   │   ├── views.py               , RunScriptPermissions + ScriptFileViewSet + ScriptProjectViewSet with its GET/PUT `script_files` action and its POST `upload` action (UploadSourcePermissions resolves POST to change, since the project exists and its source moves, and initial() re-narrows the queryset for the same reason. The Script File add permission is checked in the action, matching the Add Script page, because an upload declares a script file) + read-only ScriptProjectRevisionViewSet (NetBoxReadOnlyModelViewSet, so no write route is registered) + update-only NetBoxScriptViewSet (http_method_names drops POST and DELETE, since composing the mixins instead would drop NetBoxModelViewSet.update() and with it the changelog snapshot and the If-Match check) with its POST `run` action. Each viewset select_relates the revision its serializer nests. The run action carries its own permission class and http_method_names, because both defaults key off the HTTP method and would resolve POST to add, which no caller of a derived model holds. initial() narrows it by the run action for the same reason.
│   │   └── serializers/
│   │       ├── __init__.py        , Re-exports ScriptFileSerializer, ScriptProjectRevisionSerializer, ScriptProjectSerializer, ScriptProjectUploadSerializer, NetBoxScriptRunInputSerializer, NetBoxScriptSerializer.
│   │       ├── revisions.py    , ScriptProjectRevisionSerializer: read-only, and also the serializer event serialization resolves by model name. Omits the manifest, the script file snapshot and the validation lease fields.
│   │       ├── scripts.py      , NetBoxScriptSerializer: derived fields in read_only_fields, importable as api.serializers.NetBoxScriptSerializer for event serialization. + ScriptFileSerializer: nested project, discovery fields read-only, nested read-only revision.
│   │       ├── projects.py     , [ScriptProject] ScriptProjectSerializer.
│       │       ├── upload.py      , ScriptProjectUploadSerializer: the upload envelope. One file plus confirm_replace, and no destination field, ever. The path is the basename.
│   │       └── run.py         , NetBoxScriptRunInputSerializer: the run envelope. Variable values nest under `data`, so a variable cannot collide with an execution parameter. Refuses a past schedule and one the script class forbids.
│   ├── filtersets/
│   │   ├── __init__.py            , Re-exports NetBoxScriptFilterSet, ScriptFileFilterSet, ScriptProjectFilterSet, ScriptProjectRevisionFilterSet.
│   │   ├── projects.py             , [ScriptProject] ScriptProjectFilterSet with custom search().
│   │   ├── revisions.py            , ScriptProjectRevisionFilterSet(ChangeLoggedModelFilterSet): project by id + key, status, both digests.
│   │   └── scripts.py              , NetBoxScriptFilterSet: project by id + key, explicit MultiValueCharFilter for the TextField description, explicit MultipleChoiceFilter for the notification override, the other two overrides generated, metadata unfiltered. + ScriptFileFilterSet: project by id + key, discovery filters, custom search().
│   ├── forms/
│   │   ├── __init__.py            , [ScriptProject] Re-exports each by-type subpackage.
│   │   ├── model_forms/projects.py   , [ScriptProject] ScriptProjectEditForm + ScriptProjectScriptFilesForm (reconciles the selection onto enabled, then enqueues ProjectScriptFileRefreshJob when it moved).
│   │   ├── bulk_edit/projects.py     , [ScriptProject] ScriptProjectBulkEditForm.
│   │   ├── bulk_import/projects.py   , [ScriptProject] ScriptProjectBulkImportForm.
│   │   ├── model_forms/scripts.py    , NetBoxScriptEditForm: writable set is enabled, the three execution overrides, comments, owner, tags and custom fields. A plain NetBox model form, because every derived column is editable=False and therefore already out of it. + ScriptFileEditForm (project + source_path frozen, so disabled on edit).
│   │   ├── bulk_edit/scripts.py      , NetBoxScriptBulkEditForm: enabled only, description removed declaratively since BulkEditView setattr ignores editable=False.
│   │   ├── filtersets/projects.py    , [ScriptProject] ScriptProjectFilterForm.
│   │   ├── filtersets/scripts.py     , NetBoxScriptFilterForm. + ScriptFileFilterForm.
│   │   └── confirmations.py        , MigrationCutoverForm: the one acknowledgement the cutover will not submit without. A ConfirmationForm subclass, so generic/confirmation_form.html's hidden_fields loop has the marker it exists to render, and the visible checkbox is rendered by hand because that loop covers nothing else.
│   ├── migrations/                , [ScriptProject] 0001_initial.py; regenerate on schema change and keep the pinned deps (see Conventions).
│   ├── models/
│   │   ├── __init__.py            , Re-exports NetBoxScript, ScriptFile, ScriptProject, ScriptProjectRevision, MigrationRun.
│   │   ├── projects.py             , ScriptProject(PrimaryModel), whose Meta.permissions carries activate / migrate / reconcile beside the four standard actions, with identity/ownership invariants and script_file_candidates / declarable_script_files / select_script_files / paths_awaiting_activation + ScriptProjectRevision (immutable content fields, script file snapshot in identity, status lifecycle, validation lease fields).
│   │   ├── scripts.py              , NetBoxScript(JobsMixin, PrimaryModel): one published Script class, identity project + module_path + class_name. run_refusal_reason names the first unmet run condition and is_executable is derived from it, so none of the six surfaces that refuse a run can describe a condition that did not hold. description overrides the abstract base as an unbounded TextField, enabled (admin) separate from is_retired (sync). Three nullable execution-override columns sit beside enabled, empty meaning inherit, and the accessors resolve override then class then built-in default. scheduling_enabled takes no override, it is the author's safety claim. + ScriptFile(PrimaryModel): declared script files, canonical importable source_path frozen with project after creation, sibling rejection by letter case and by module name, system-managed discovery fields.
│   │   └── migration.py           , MigrationRun(ChangeLoggedModel): one attempt at moving off the built-in feature. Migration infrastructure rather than a domain model, so no REST, GraphQL or list view. State moves forward one step only, at most one run is open, and the journal is what every cutover step replays from. complete_step() and recorded_counts() are the one home of the resume guard every caller needs. record_journal(), record_step() and record_warnings() are the only writers of the two JSON columns and each merges what the row holds with what the caller has under migration_lock(), so a pass that has not saved its own entries yet keeps them and one recorded by another object is not dropped.
│   ├── tables/
│   │   ├── __init__.py            , Re-exports ScriptFileTable, ScriptProjectFileTable, ScriptProjectRevisionScriptFileTable, ScriptProjectRevisionTable, ScriptProjectTable, NetBoxScriptTable.
│   │   ├── projects.py                 , [ScriptProject] ScriptProjectTable(PrimaryModelTable) + ScriptProjectRevisionTable(BaseTable), the history table with no list view. Its ActionsColumn carries only extra_buttons and needs exempt_columns to render, since BaseTable hides unselected columns, and its script_file_count column is derived from the snapshot with no extra query, because two revisions can share a source digest and differ only there. + ScriptProjectFileTable, the Revision Files tab fed the manifest's dictionaries rather than a queryset + ScriptProjectRevisionScriptFileTable, where that count resolves, fed the script file snapshot the same way.
│   │   └── scripts.py              , NetBoxScriptTable + NetBoxScriptLogTable, the run log fed a list of dictionaries rather than a queryset. + ScriptFileTable: source_path is the linked column, revision column unlinked.
│   ├── tests/                     , Each area mirrors its module layout (flat file or subpackage).
│   │   ├── __init__.py            , [stub] Test discovery anchor.
│   │   ├── plugin_testing.py      , [shared] Plugin-aware view/API test mixins (always rendered). PrimaryObjectViewTestCase + NestedObjectViewTestCase + DerivedObjectViewTestCase, the last for models whose rows are derived, so no create, delete, or import. Also the one home of ChangeLoggedFilterSetTestMixin, which the three filterset suites import from here rather than from the host.
│   │   ├── models/__init__.py     , [ScriptProject] Test package anchor.
│   │   ├── models/test_projects.py , [ScriptProject] ScriptProjectTestCase: create, str, absolute_url, data_path canonicalization, immutability + constraint invariants.
│   │   ├── api/__init__.py        , [ScriptProject] Test package anchor.
│   │   ├── api/test_projects.py , [ScriptProject] ScriptProjectAPIViewTestCase(PluginAPIViewTestCases.APIViewTestCase).
│   │   ├── views/__init__.py      , [ScriptProject] Test package anchor.
│   │   ├── views/test_projects.py , [ScriptProject] ScriptProjectTestCase(PluginTestCases.PrimaryObjectViewTestCase), plus the source-state, Activate and Repair view suites. The Repair one covers the inert button, the two message branches and the permission gate, and it is the only suite that reaches the already-active promotion path through a request.
│   │   ├── tables/__init__.py     , [ScriptProject] Test package anchor.
│   │   ├── tables/test_projects.py , [ScriptProject] ScriptProjectTableTestCase(TableTestCases.StandardTableTestCase) + RevisionScriptFileColumnTestCase, which asserts the count on a table built per revision, since a table cannot be ordered by a column it does not declare.
│   │   ├── forms/__init__.py      , [ScriptProject] Test package anchor.
│   │   ├── forms/test_projects.py , [ScriptProject] EditForm / FilterForm / BulkImportForm test cases.
│   │   ├── filtersets/__init__.py , [ScriptProject] Test package anchor.
│   │   ├── filtersets/test_projects.py , [ScriptProject] ScriptProjectFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests).
│   │   ├── graphql/__init__.py    , [ScriptProject] Test package anchor.
│   │   ├── graphql/test_projects.py , [ScriptProject] ScriptProjectGraphQLTestCase: enum members match the ChoiceSets.
│   │   ├── models/test_scripts.py  , NetBoxScript identity, retirement, cascade + is_executable. + ScriptFile model invariants.
│   │   ├── api/test_scripts.py     , NetBoxScriptSerializer route reversal, event serialization, patchable enabled, ignored derived fields, refused create/delete. + ScriptFileAPIViewTestCase: read-only discovery fields, path canonicalization + refusals.
│   │   ├── api/test_revisions.py   , Revision serializer resolution by model name, rendering without a route, REST delete of an activated project.
│   │   ├── api/test_run.py        , The scripts/<id>/run/ contract: the envelope, the Job response, the refusals, and that neither the add permission nor an object constraint can be bypassed.
│   │   ├── views/test_scripts.py   , NetBoxScriptViewSetTestCase(PluginTestCases.DerivedObjectViewTestCase) + detail view, changelog rendering, and the absent create/delete routes. + ScriptFileTestCase(PluginTestCases.NestedObjectViewTestCase).
│   │   ├── views/test_run.py      , The run page, the run permission gate, the queued payload, the result page and its level threshold, plus one end-to-end submit-and-execute.
│   │   ├── views/test_actions.py  , Static guard: every list, detail and row action is checked against the registered routes, because ActionsMixin and ActionsColumn filter by permission alone.
│   │   ├── views/test_files.py    , The Revision Files tab: manifest rows, the script file marker, both missing-path annotations (gone from the source versus held only by a newer revision), the empty state, and the view gate.
│   │   ├── views/test_migration.py , The Migration page and its seven enqueue routes: both permission gates, every enqueue, the queued-pass refusals, and which button each state renders.
│   │   ├── views/test_migration_run.py , One migration's detail page.
│   │   ├── models/test_migration_run.py , MigrationRun: the state machine, the single-open-run rule, and the journal helpers.
│   │   ├── tables/test_scripts.py  , NetBoxScriptTableTestCase(TableTestCases.StandardTableTestCase). + ScriptFileTableTestCase(TableTestCases.StandardTableTestCase).
│   │   ├── filtersets/test_scripts.py , NetBoxScriptFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests), metadata in ignore_fields. + ScriptFileFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests), every field filterable.
│   │   ├── forms/test_scripts.py   , Edit + bulk edit forms: the writable set, and that a stale save cannot revert a derived field. + Edit / Filter form test cases.
│   │   ├── graphql/test_scripts.py , NetBoxScriptGraphQLTestCase: last_seen_revision absent from the type. + ScriptFileGraphQLTestCase: discovery enum matches the ChoiceSet, the revision relation resolves.
│   │   ├── views/test_revisions.py , Activate/Deactivate buttons: round trip, refusals, permissions, and which button each status renders. Plus RevisionScriptFilePanelTestCase, the frozen paths and the empty case.
│   │   ├── views/test_reconcile.py , The Reconcile Source action: the confirmation, the enqueue, the permission gate, and which source type renders the button.
│   │   ├── forms/test_confirmations.py , MigrationCutoverForm: the box is required, and it is visible rather than hidden, since the confirmation template renders only hidden fields.
│   │   ├── forms/test_script_files.py , Selection reconciles onto enabled, nested paths, missing declared paths.
│   │   ├── api/test_script_files.py , The projects/<id>/script-files/ GET + PUT contract.
│   │   ├── api/test_upload.py , The projects/<id>/upload/ contract: the refusals that answer 400, the 201 that carries an already-invalid revision because the route bounds one file while the manifest reads the tree, what each activation policy leaves behind, and both halves of the permission pair.
│   │   ├── views/test_upload.py , The Upload page, which creates a Project from one script, and the Add Script page, which stages the existing tree plus the new file. Covers the one-shot activation tick travelling to the validation job separately from the standing policy the Project persists, the confirmation a known path and a shared basename both need, and that a rolled-back upload leaves the store untouched.
│   │   ├── models/test_script_file_candidates.py , Candidate enumeration from DataFile and from the newest manifest.
│   │   ├── graphql/test_revisions.py , ScriptProjectRevisionGraphQLTestCase: status enum matches the ChoiceSet, stored documents and lease fields stay off the type.
│   │   ├── storage/               , Storage tier suites: config, paths, manifest, script_files, store, service, signals, jobs, branching, backend contract, concurrency. test_concurrency.py really commits, so its on_commit callbacks enqueue live RQ jobs. It shares that shape with MigrationLockTestCase in tests/models/test_migration_run.py, and both prove serialization in two halves against a second connection rather than by racing threads, which cannot be made deterministic. It drains the queue in cleanup so the suite leaves nothing behind even in the isolated database.
│   │   ├── runtime/               , Runtime tier suites: test_cache.py, test_naming.py, test_loader.py, test_discovery.py, test_introspection.py, test_resolution.py.
│   │   ├── scripts/               , Authoring API suites: test_base.py, test_variables.py, test_exports.py.
│   │   ├── compat/                , Legacy compatibility suites: test_imports.py (compat_import per statement form, both sides of the transition, no database) and test_dialects.py (every form end to end out of one validation pass).
│   │   ├── migration/             , Migration tier suites: test_source.py (the read seam against real built-in rows), test_dialects.py (classification per import statement form, no database), test_plan.py (grouping including the nested-folder collapse, the findings, and the inventory job), test_staging.py (both source types end to end, idempotence, and that no staged revision activates), test_mapping.py, test_cutover.py, test_activation.py, test_references.py, test_history.py, test_cleanup.py (the per-module refusals, which are the safety net the whole ordering exists for), test_verification.py. LegacySourceMixin in test_staging.py is the shared built-in fixture and the mixins chain off it, so a suite reuses the fixture without re-running the borrowed suite's tests. It points the 'scripts' storage alias at a real temporary directory, never in-memory, because ManagedFile.storage is a fresh instance while the loader reads the cached one.
│   │   ├── test_execution.py      , run_script() suites (commit and dry-run, both abort classes, failure logging, request-processor selection and isolation, the current-request restore) plus NetBoxScriptJob suites (pinning, the administrative recheck, resolution failures, run-record sanitization).
│   │   ├── test_validation.py     , Validation service + RevisionValidationJob suites (claim/reclaim, fencing, classification, sanitization, Script File persistence).
│   │   ├── test_activation.py     , SynchronizeScriptsTestCase (upsert/retire/no-op-write semantics, and that the reported count agrees with the queries issued) + ActivateRevisionTestCase + PromotionCallbackTestCase (required callback, savepoint depth, rollback).
│   │   ├── test_ingestion.py      , Ingestion ordering and failure modes for both callers, plus UploadToActiveTestCase and DataSourceToActiveTestCase: each slice end to end against real validation.
│   │   ├── test_script_file_refresh.py , The selection-change path: the enqueue, the Job body, the form's changed-only rule, and the activation policy end to end.
│   │   ├── test_inherited_guarantees.py , Guarantees delivered by a base class rather than by our code, each asserted as behaviour so an override drops it loudly: a read-only token cannot start a run (TokenPermissions, which enforces it from BOTH has_permission and has_object_permission, so a control has to bypass both), and a raising action neither propagates nor ends the dispatch batch (is_plugin_provided, which register_event_rule_action assigns onto the INSTANCE, so a class attribute is overwritten and only the registered instance decides).
│   │   ├── test_internals_canary.py , The canary script loaded by path: every listed crossing resolves on this host, the resolver reports a missing attribute, module or registry key under its row, refuses a malformed probe rather than resolving part of it, the lock check keys on the plugin's real namespaces, a plugin NetBox skipped at settings load is reported, and the page's table and the probe list agree row for row, every row carrying a probe.
│   │   ├── test_permissions.py    , The source-management separation: change alone cannot activate or reconcile, each own action can, and an object constraint narrows both projects and their revisions.
│   │   ├── test_event_rules.py    , The `netbox_scripts.run` action: registration, the refusals validate() makes and the ones it leaves to dispatch, the event payload, and import resolution by project key. Skipped entirely below the 4.7 line through an importlib.util.find_spec probe, which is the only guard the feature needs. One class reads the queued task, because enqueue_run keeps script input off the Job row so that is the only place action_data can be seen.
│   │   ├── test_event_sources.py  , The plugin's models as Event Rule sources: all four qualify, a rule saves against one, the webhook body carries identity and no stored document, and a matching rule reaches the queue. The queue is isolated in testing/configuration.py rather than per class, so emptying here only separates one test from the next. Never clear with RQQueueTestMixin, which uses a server-wide flushall(). Dispatch needs captureOnCommitCallbacks, since django_rq defers an enqueue to on_commit and a TestCase never commits.
│   │   ├── test_management.py     , The runcustomscript command: what it resolves, what it refuses, that a committed run is change logged against the named user, and that a failure raised before the script is reached still reports why.
│   │   └── test_reconciliation.py , The post_sync receiver (which projects, and that it never fails a sync) plus ProjectReconciliationJob, including the reverted-directory activation.
│   ├── views/
│   │   ├── __init__.py            , [ScriptProject] Re-exports every view class except the three shared bases, `__all__` alphabetised.
│   │   ├── migration.py            , The Migration page, the run's detail view, and the seven enqueue views, on TWO gates. BaseMigrationView takes add_scriptproject for the page, the inventory, staging and verification, because creating Projects is all those authorize. DestructiveMigrationView takes migrate_scriptproject for the cutover, activation, repoint and cleanup, because closing and deleting rows of the built-in feature is not a form of creating a Project. The cutover is offered while the crossing is unrecorded rather than while the state reads staging, so the one button that finishes an interrupted crossing stays up, and a notice beside it says the fence may already have closed. Staging, the cutover and cleanup confirm first and refuse while a pass of their own class is queued, the inventory and verification do neither, because they write nothing. The repoint button is withheld, with the Projects named, while any of them serves nothing, gated on the frozen map rather than on the recorded step because that is what the predicate reads.
│   │   ├── projects.py                  , [ScriptProject] List/Detail/Edit/Delete/BulkEdit/BulkDelete/BulkImport views, the Script Files tab, the read-only Revision Files tab, the Revisions history tab, Upload (create) + Add Script (detail) upload views, and the Activate, Repair and Reconcile confirmation views. Activate reports through views/revisions.py's shared activation_message(), so the two Activate routes cannot word the same outcome differently. Repair re-activates the revision already in force, which is the only route to the already-active path, because the per-revision Activate view now filters its queryset to ACTIVATABLE_REVISION_STATUSES and the project-level one resolves through activatable_revision(). Withholding the button was not enough: an action filters by permission and never by route. Its queryset is narrowed to projects serving something, and it reports a repair and a no-op differently, which is the defect it exists for. Reconcile narrows its queryset to Data Source-backed projects, so the route does not apply to an uploaded one.
│   │   ├── scripts.py               , List/Detail/Edit/BulkEdit views plus Run (GET builds the class's own form out of the active revision, POST enqueues) and Result (one run's log, read out of the Job). No add, delete, bulk delete or bulk import: rows are derived from an activated revision, and retirement replaces deletion. The Jobs tab needs no view, JobsMixin registers one. Run declares a ViewTab gated on the run permission, so every view of the script offers it, and builds its form through execution.load_script_class(), which the REST run action shares. Result serves its body as a partial to an htmx poll, so a run that has not reached a terminal state refreshes itself, at a slower rate while it is only scheduled. + List/Detail/Edit/Delete/BulkDelete views. No bulk edit or bulk import: selection happens on the Project.
│   │   └── revisions.py             , Detail view for one revision, carrying the script file paths its snapshot froze, which is where the Revisions tab's script file count resolves. That table always renders, unlike the problems one, because an empty snapshot is why a revision publishes nothing. Plus Activate + Deactivate, GET confirms and POST performs, and activation_message(), the one wording both Activate routes report through. Gated on the PROJECT's activate permission, with the revision queryset narrowed to permitted projects. The tab links here rather than posting: its table is inside the bulk-action form, so a nested form would submit the outer one.
│   ├── ui/
│   │   ├── __init__.py            , [ScriptProject] Re-exports ScriptProjectPanel + ScriptProjectSourcePanel.
│   │   └── panels.py              , [ScriptProject] ScriptProjectPanel (left) + ScriptProjectSourcePanel and ScriptProjectStatePanel (right) for the detail view layout, plus NetBoxScriptPanel and NetBoxScriptStatePanel, the two Script File panels, and the two Revision panels. source_state sits on the Project panel rather than the State panel, because it is keyed on the NEWEST revision of all while every field of the panel titled 'Current revision' reads off current_revision, which is the active revision or, failing one, the newest stored revision. The two are often different revisions.
│   ├── search.py                  , [ScriptProject] NetBoxScriptIndex + ScriptFileIndex + ScriptProjectIndex, each registered via @register_search.
│   ├── graphql/
│   │   ├── __init__.py            , [ScriptProject] Exports schema = [Query].
│   │   ├── schema.py              , [ScriptProject] @strawberry.type(name='Query') with netbox_script_project / netbox_script_project_list fields.
│   │   ├── types.py               , [ScriptProject] ScriptProjectType(PrimaryObjectType); choice fields expose raw string values.
│   │   ├── filters.py             , [ScriptProject] ScriptProjectFilter(PrimaryModelFilter) with enum-typed choice filters; no storage_key filter.
│   │   └── enums.py               , [ScriptProject] ProjectSourceTypeEnum + ActivationPolicyEnum via strawberry.enum(ChoiceSet.as_enum()).
│   ├── storage/
│   │   ├── config.py              , Resolves the required STORAGES['netbox_scripts'] backend and limit settings.
│   │   ├── paths.py               , Canonical source paths, the case-insensitive comparison, compiled-artifact refusal, storage keys.
│   │   ├── manifest.py            , Manifest build/validate pair, content digests, per-node letter-case collision rejection.
│   │   ├── script_files.py         , Script file snapshot build/validate pair, the return-trip trust boundary.
│   │   ├── store.py               , Verified writes and reads against the backend, copy_verified bounded-read primitive, read_verified / read_revision_tree in-memory reads, present_keys exact-key existence probe for the storage sweep.
│   │   ├── locks.py               , project_lock(): the per-project advisory lock every content operation holds, taken through django_pg_utils.advisory_lock, the helper NetBox ships and takes its own locks through. This module owns the key derivation and the (770100, .) namespace, with models/migration.py deriving (770101, 1) from it. The separation from core is numeric and not by arity, since core takes two-integer pairs too, so docs/development/netbox-internals.md records both values against ADVISORY_LOCK_KEYS.
│   │   ├── service.py             , stage_revision / refresh_revision_script_files / promote_revision (mandatory on_promote callback) + the database-alias contract.
│   │   └── exceptions.py          , Storage error taxonomy.
│   ├── runtime/
│   │   ├── cache.py               , Manifest-verified local materialization of revision trees, the one sanctioned local-write tier.
│   │   ├── loader.py              , Private-namespace package loader: import sessions, failure sweep, unload.
│   │   ├── naming.py              , Private module names + the script file dotted-name adapter.
│   │   ├── discovery.py           , discover_scripts(): publication rules, script_order, identity + logger markers.
│   │   ├── introspection.py       , describe_script / validate_discovered_scripts: forces run-form construction, the JSON-safe published-script record.
│   │   ├── resolution.py          , resolve_script_class(): a stored identity back to a live class through the snapshot's script file provenance. Opens no import session and never unloads, the caller runs what it returns.
│   │   └── exceptions.py          , Runtime error taxonomy (cache, module path, import, discovery, resolution).
│   ├── scripts/                   , Authoring API: base.py (BaseScript/Script, whose scheduling_offered ANDs the class's scheduling_enabled with the caller-set scheduling_permitted, so the fieldsets and the form cannot disagree, and which carries the two run-context attributes the runner binds: request, and event for a run an Event Rule drove), variables.py, forms.py (ScriptForm carries the four execution parameters, and drops the two scheduling fields when scheduling is not offered), logging.py, exceptions.py.
│   ├── compat/
│   │   ├── __init__.py            , install() + the name-scoped finder + wrap_loader + compat_import + the extras stand-in + the host probe. One rule: inside revision code the name `extras` resolves through the stand-in.
│   │   └── legacy.py              , Report, carrying the marker discovery refuses, so an unsupported dialect produces a message rather than a Run button that raises.
│   ├── migration/
│   │   ├── __init__.py            , Tier docstring only, like storage/ and runtime/. No re-exports.
│   │   ├── source.py              , The ONLY module that reads the built-in Custom Scripts feature, so a supported export service is a change to one file. Read-only, returns frozen dataclasses and plain counts. Filters to the SCRIPTS root: the proxy manager admits reports as well, and a report's bytes live under REPORTS_ROOT while every read here goes through the scripts backend, so one could never be read. Reports are outside this migration entirely and legacy_report_count() is what makes that visible. Opens `file_path` rather than `full_path`, and resolves both content types with for_concrete_model=False because a Job and an Event Rule record the proxy.
│   │   ├── dialects.py            , ast-based classification of stored source into native / legacy_import / report_style / unparsable, plus defines_a_script(), the shape test staging and the inventory share. Imports nothing: an inventory must not execute an operator's code to classify it. Report shape outranks a legacy import, because the two cost an author different work.
│   │   ├── plan.py                , The grouping rule and the inventory report. Pure functions over seam output except for the existing-Project check, which is the one part that reads plugin rows and therefore runs only in live mode. It also reads each module's own source, blocking an absolute import that resolves to neither the standard library nor an installed distribution, since a plain import never reaches a file beside it and such a module can never reach a verdict, and warning where the built-in feature publishes a class the source does not itself define. An import the module already guards against being absent is left alone. One Project per folder that holds scripts, and _collapse() merges a folder into the shallowest script-holding folder containing it, because the model refuses two overlapping data paths on one source and the higher Project's tree already holds the deeper file.
│   │   ├── mapping.py             , Which Script one built-in Custom Script becomes, derived rather than stored. module_path is the DOTTED name off cls.__module__, so the identity composes the collapse rule with source_path_to_dotted_name().
│   │   ├── staging.py             , Creates the proposed Projects and delegates to ingestion. Every Project takes the MANUAL policy, since validation would otherwise activate under any other one. Validates rather than get_or_create's, so a path overlapping a hand-made project is refused instead of written, and a reused Project not on the manual policy is refused too, since staging onto it would let validation put the built-in modules into service. A refusal is returned as a result entry rather than raised, so one Project the model will not accept does not cost the operator the rest of the pass. Declares only a member that would publish, by its built-in Custom Script rows or by its source shape, because a revision whose script files all publish nothing is refused and the Project could then never be activated.
│   │   ├── cutover.py             , The irreversible step: capture every reference the later passes replay, plus the plugin map they all resolve through, then close what a plugin can. Captures once, because a second capture would read the closed state back as the original. The state advances between the capture and the closures: after the capture, so a run a failure leaves behind is one staging can still take back, and before the closures, so no crash can leave the installation fenced while the state still reads staging. Also unservable_projects(), the precondition the fence refuses on, whose predicate mirrors _activate_project rather than a status tuple, because that tuple admits retired and activation refuses it. Also activate_staged(), which comes after the fence because an Event Rule's action object has to name a NetBoxScript that exists, and projects_not_serving(), the live answer to whether it succeeded, which the reference passes, cleanup and the page all gate on rather than on the recorded step. Absent is not the same as not serving there: a Project an operator deleted past the fence drops out, so the passes proceed and cleanup refuses its modules by name instead of every remaining pass refusing forever. The fence's own refusal reads the newest revision's validation job, so a module that can never be judged is named rather than reported as a verdict still to come.
│   │   ├── references.py          , Repointing Event Rules, permissions, Job history and schedules onto plugin rows. ACTION_SLUG is declared here and never imported from event_rules.py, so this tier does not become a second loader of a module PluginConfig is meant to resolve on its own. Each step records completion only once it has left nothing a re-run could still move, because the step record is what makes the next run skip it. Permissions is the exception twice over. It records whatever it leaves, since a constrained or unmappable grant is for a person to recreate, and it does not take the serving gate the other three do, because the fence withdrew every grant on the built-in feature and blocking it over an unrelated Project would keep every non-superuser locked out of both sides. The same split runs inside the other three, and the test for permanent is whether a routine fix and a re-run could still settle it, or only a person redoing it by hand. A class that left its file is absent from build_map by construction, so its history, an Event Rule naming it and a schedule naming it are all permanent, as are a module Job, a past-due schedule, a schedule whose owner was deleted or deactivated, and input naming a deleted object. Everything else is operator-fixable, retirement included, since synchronize_scripts clears is_retired on every republish.
│   │   ├── cleanup.py             , The last step, and the only one that deletes. Scoped by the map the cutover froze, never by build_map(), whose keys shift as modules are deleted. Gated on all four reference steps, because a captured schedule is recreated through the rows this pass removes. Refuses per module when a class resolves to nothing or to a retired Script, since the plugin will not run a retired row and deleting the module would take the last runnable copy, and when deleting it would destroy Job history or an Event Rule, because both reach it through a GenericRelation the collector follows, and sorts those refusals into RETAINED and BLOCKED. Only blocked holds the run open: a module holding history of its own, or history for a class that left the file, is permanent, and treating it as outstanding would leave a migration that can never close and no replacement that can ever open. The stranded refusal is tested ahead of the two history ones it can accompany, because it is the only one naming an action the operator can take.
│   │   └── verification.py        , The five read-only checks, each naming which side it read. A recreated schedule is resolved against the live Job rows, and against the queue too while it is still waiting, because the row survives a crash the task does not. Reads with rq's own Job.fetch, never Queue.fetch_job(), which writes on a miss. A queue it cannot reach is a warning naming the journal alone, never a loss. Reads the LATEST run rather than the open one, since current() excludes migrated and a finished run is the one worth verifying. A reference the journal captured and the repoint left behind is a warning, one absent from the journal appeared after the cutover and is a genuine fault, as is a schedule the journal claims that the queue never received. An Event Rule whose action names a built-in script module rather than a Script is left withdrawn: the two sit in different tables with their own sequences, so an id alone cannot say which one it meant.
│   ├── branching.py               , NetBox Branching integration: GLOBAL_MODELS main-schema routing for all five models, safety checks.
│   ├── execution.py               , run_script(): the transaction, request-processor and event context one run happens inside. The only home of the six undocumented NetBox symbols execution needs, so the requested generic core context replaces one file. Also load_script_class(): a stored row to a live class through the revision its project serves, unloaded before it returns, so the class is good for introspection rather than a run. Model-aware, which is why it is here and not in the runtime tier.
│   ├── validation.py              , validate_revision(): lease claim, fenced verdicts, error classifier, sanitizer, Script File result persistence, published-script record.
│   ├── activation.py              , activate_revision() / deactivate_revision() domain orchestrators + synchronize_scripts(): the NetBoxScript upsert-and-retire pass, no imports. synchronize_scripts() returns the number of rows it wrote and activate_revision() carries it out as an ActivationResult, collected in a closure around the callback rather than by widening promote_revision(), whose return is the storage tier's contract. deactivate_revision() still returns the revision alone, because deactivation has one outcome the confirmation page already states.
│   ├── jobs.py                    , ProjectStorageCleanupJob (cleanup rechecks references under the project lock) + ProjectStorageSweepJob (the daily sweep that reports what an unfinished cleanup left in the store, reclaiming nothing. The only job here core schedules rather than a caller enqueueing it, through @system_job, so it needs no trigger of its own, and it cannot set a job timeout that way either, which is why it takes the oldest candidates first and puts the report on the row before the loop. Groups candidates by stored tree, since a Project cascade records one cleanup per revision and rows can share a digest. It takes no routing check for the same reason the migration inventory takes none, it writes nothing. Content no cleanup Job names is out of its reach, because finding that needs a backend that can enumerate) + ProjectReconciliationJob (stages the Data Source directory as it stands at run time, and activates what a reverted directory resolves to) + ProjectScriptFileRefreshJob (restages the stored tree under the current selection, the only route an uploaded project has to apply one) + RevisionValidationJob (activates through activation.activate_revision on a valid verdict when the policy allows, or when the enqueue asked for this one revision) + NetBoxScriptJob (pins the revision at enqueue for a one-shot run and pins nothing for a recurrence, which resolves the active revision per occurrence because JobRunner re-enqueues a periodic job with the same kwargs, then resolves the class out of it, runs it through execution.run_script, and sanitizes the run record before it reaches the Job row, and carries an optional event payload from enqueue onto the instance, which must stay JSON-safe because it rides on the Job row) + the seven migration jobs: MigrationInventoryJob (reports what a migration would do, writing nothing) + MigrationStagingJob (refuses on any blocking finding, then stages the proposed Projects) + MigrationCutoverJob (captures, then closes what a plugin can) + MigrationActivationJob (puts the staged Projects into service, so the NetBoxScript rows a reference can name exist) + MigrationReferencesJob (Event Rules, permissions, Job history and schedules, in that order, history before schedules so preservation happens before anything new is created) + MigrationCleanupJob (deletes the mapped modules, skipping any whose deletion would take Job history or an Event Rule with it) + MigrationVerificationJob (the five read-only checks). Every migration job imports the migration tier locally, because staging reaches ingestion, which imports this module. Each is started from the Migration page, which is the only trigger any of them has. All but the inventory and the verification check branching.unsafe_routing_reason() first, because enqueue-time safety does not carry to a job that may run much later on another pod, and those two need no check because they write nothing.
│   ├── signals.py                 , Revision deletion enqueues storage cleanup, and a completed Data Source sync enqueues one reconciliation per project on it. Wired in AppConfig.ready().
│   ├── event_rules.py             , RunNetBoxScriptAction, the registered `netbox_scripts.run` action, plus the `event_rule_actions` list PluginConfig loads by convention, so no AppConfig entry is needed. A thin adapter onto NetBoxScriptJob.enqueue_run(), which already owns pinning and the executability check, and it reports a script it cannot run rather than raising, because one rule's misconfiguration must not end the batch. validate() refuses only retirement, since a disabled script is temporary state an administrator flips back. **This module needs no version guard and must not grow one**: PluginConfig resolves the list only where DEFAULT_RESOURCE_PATHS carries the key, which no 4.6 release does, so nothing below the 4.7 line ever imports it.
│   ├── choices.py                 , ProjectSourceTypeChoices, ActivationPolicyChoices, RevisionStatusChoices, FileDiscoveryStatusChoices, MigrationStateChoices.
│   ├── validators.py              , [ScriptProject] normalize_data_path(): canonical data_path form, shared by model clean() and the REST serializer.
│   ├── utils.py                   , source_path_to_dotted_name(): the one home of the path-to-module rule. data_source_relative_path(): the one home of the segment-wise data_path rule, shared by candidate listing, ingestion and migration staging.
│   ├── constants.py               , Storage limits, revision status groupings, validation lease bounds, published-script field bounds.
│   ├── management/commands/runcustomscript.py , The shell route to one run, for a self-hosted operator. Additive only: it duplicates the REST run route, which is what Cloud and Enterprise use, so it carries a cloud-compat waiver rather than breaching the contract. Named runcustomscript because NetBox ships its own runscript until v5.0 and the earlier app in INSTALLED_APPS wins the name, so a plugin command called runscript would never be reachable. Runs immediately in the calling process and exits non-zero unless the Job completed, which the built-in command never did. Refuses a --user that matches nobody rather than falling back to the first superuser.
│   ├── ingestion.py               , ingest_upload() (which takes activate_once, the upload form's one-shot, and forwards it to the validation job so a manual project can still put one upload into service) / ingest_data_source() / current_source_tree() / uploaded_source_path() / declare_script_file() / check_upload_conflicts(): the one home of source ingestion. check_upload_conflicts() holds the replacement and case-collision rules, so the Add Script form and the REST upload action cannot drift. Upload declares the file it carries unless its caller passes declare=False, which only migration does for a module that would publish nothing, and a synchronized directory declares nothing. declare_script_file() is public because migration staging shares it, so the rule that a declaration is reused rather than replaced has one home.
│   ├── object_actions.py          , ActivateRevision + AddScript + ReconcileSource + RepairScripts + RunScript ObjectAction subclasses, with button templates under templates/.../buttons/. Each takes the model's own action rather than change: activate, reconcile and run. RepairScripts takes activate too, because republishing rows is the write activation makes. RunScript, RepairScripts and AddScript render inert rather than hidden when they cannot act, AddScript because an upload declares a Script File and permissions_required cannot name another model. **The explanatory title goes on a wrapping span, never on the disabled button**, because Tabler sets pointer-events:none on both .btn:disabled and .btn.disabled, so a title on the control itself never surfaces. AddScript and ReconcileSource each render only for the source type they belong to.
│   ├── template_content.py        , [add as needed] PluginTemplateExtension classes (cross-model UI).
│   └── templates/netbox_scripts/
│       ├── scriptproject.html              , [ScriptProject] Detail-view template, extends `generic/object.html`.
│       ├── scriptprojectrevision.html            , Detail-view template for one revision, extends `generic/object.html`.
│       ├── migration.html         , The Migration landing page, which extends `generic/_base.html` rather than an object template. Lists the last inventory's blocking findings above the buttons, because staging refuses on any of them and creates nothing, and counts the warnings instead, because that list is one entry per module.
│       ├── migrationrun.html      , Detail-view template for one migration attempt.
│       ├── migration_stage.html   , Confirmation for staging.
│       ├── migration_cutover.html , Confirmation for the fence. Says there is no way back, in those words.
│       ├── migration_cleanup.html , Confirmation for the deletion, naming that the stored source goes with it.
│       └── *.html                 , [add as needed] Per-model detail templates and bulk-action forms.
├── docs/                          , mkdocs site (zensical primary, mkdocs compatible).
├── scripts/
│   ├── check_cloud_compat.py      , AST checker for the Cloud / Enterprise platform contract (pre-commit hook).
│   └── check_netbox_internals.py  , Resolves every crossing docs/development/netbox-internals.md lists after django.setup() with no database, refuses a core advisory-lock key on either plugin namespace, and fails when NetBox skipped the plugin outside its version range. CI runs it against NetBox's feature branch as the one blocking job on that ref.
├── testing/
│   └── configuration.py           , NetBox config used by the test workflow (maintainer-added; see Development). **Points Redis at databases 15 and 14, not the 0 and 1 a running NetBox uses, and is the one home of that isolation.** The test runner isolates the database but nothing isolates Redis, so a committing TransactionTestCase enqueues a live RQ job whose kwargs carry pickled model instances holding TEST primary keys, which any worker on the host then inserts verbatim into whichever database it serves.
├── .github/workflows/             , test.yml, release.yml, claude-review.yml.
├── AGENTS.md                      , This file. Source of truth for AI agents.
├── CLAUDE.md                      , Shim that pulls in AGENTS.md.
├── COMPATIBILITY.md               , Plugin → NetBox version matrix.
├── LICENSE.md                     , NetBox Limited Use License 1.0.
├── README.md                      , Project README.
├── SECURITY.md                    , Security policy.
├── mkdocs.yml                     , Docs site config (zensical and mkdocs).
├── pyproject.toml                 , Plugin metadata + dependencies.
└── .pre-commit-config.yaml        , Local quality gates.
```

## Architecture

### Domain model

Four domain models, plus `MigrationRun`, which is migration infrastructure rather
than domain content. All five are installation-global: `GLOBAL_MODELS` in
`branching.py` routes every one of them to the main schema under NetBox
Branching. `ScriptProject`:
one project = one script source tree = one Python package boundary, owning
either uploaded content or a Data Source directory, never both, with frozen
identity fields (`key`, `source_type` immutable, `storage_key` never changes).
`ScriptProjectRevision`: one immutable snapshot of the tree plus the
script file configuration it was staged under, identity = project + source
digest + script file digest, moved through its lifecycle by the storage and
validation services only. `ScriptFile`: one declared
script file per row, author-editable declaration fields, system-managed
discovery fields, enabled declarations frozen into each revision's script file
snapshot at staging time. `NetBoxScript`: one published Script
class per row, parented on the **project** rather than the Script File, because
`script_order` lets a helper-defined class publish and helpers have no Script File
row, so the publishing script file is provenance in the revision snapshot instead.
Rows are derived from an activated revision, never authored: `enabled` is the
administrator's and synchronization never writes it, while retirement replaces
deletion so accumulated Job history survives.

### Source ingestion

`ingestion.py` is the one entry point that turns supplied files into a revision
on its way to a verdict: it declares the script files the source implies, stages
the tree, and enqueues validation. Ordering is load bearing, because a revision
freezes the project's enabled declarations into its script file snapshot at
staging time, so declarations are committed before staging. The content write
stays outside that transaction, since a rollback around stored bytes would leave
content no revision row names and therefore nothing to record its cleanup.

It has two callers, and they differ only in what the source implies. Upload
(a create form for a new project, an Add Script view for an existing one)
declares the file it carries, because every file a person uploads is a
script file. Migration is the one caller that says otherwise, passing
`declare=False` for a built-in module that would publish nothing, since a
revision whose script files all publish nothing is refused.
`ingest_data_source()` declares nothing, because a Python file that appears in a
repository is a candidate somebody selects rather than something to publish on
arrival, and staging freezing only the enabled declarations is what carries a
selection across a synchronization. It stages the complete directory every time,
so a deleted file is simply absent from the next revision, and it skips compiled
artifacts while leaving every other refused path to the manifest, which records
it on an invalid revision. Activation is not a separate mechanism: the validation
job promotes a valid revision when the project's `activation_policy` says so, and
the Activate view covers the manual policy.

A completed Data Source synchronization reaches ingestion through a `post_sync`
receiver that enqueues one `ProjectReconciliationJob` per project on that source
and does nothing else, because the committing process performs no storage I/O and
core sends that signal with `send()` as the last statement of `DataSource.sync()`.
The receiver therefore swallows and logs its own failures rather than failing an
operator's synchronization. Two synchronizations in quick succession enqueue two
jobs and need no coalescing: the second stages identical content, resolves to the
revision the first created, and enqueues no second validation. One case has no
verdict to wait for. A directory reverted to a tree the project held before
resolves by content addressing to the revision that already validated it, which
validation can no longer claim, so the job activates it directly under the same
policy check.

### Legacy authoring compatibility

The whole compatibility surface for the built-in authoring API is one import line, so
`compat/` closes it without rewriting stored source. Revision modules execute with
their own builtins mapping whose `__import__` resolves the name `extras` through a
plugin-owned stand-in serving `scripts` and `reports`. The whole name resolves, not
the two dotted ones, because `from extras import scripts` and a bare `import extras`
both compile with `extras` as the imported name and would otherwise fall through to
the host silently.

Scoping is by module name, not by time: a `sys.meta_path` finder matching only names
below `runtime.naming.PRIVATE_ROOT` wraps each revision loader. Nothing process-wide
is rebound and there is no window, so concurrent workers need no coordination and no
host module is ever replaced. The revision root is registered by hand rather than
found, which is why `runtime/loader.py` wraps its loader explicitly.

The redirect is transitional and the clock is the host, never a version literal. Once
NetBox drops a legacy name the seam keeps intercepting and raises `ImportError` naming
the migration, because standing aside raises `ModuleNotFoundError`, which classifies as
`environment` and fails the validation job with no verdict recorded. Report-style
classes are refused at discovery for the same reason. Silence is what this tier
removes, so it trades none of it back.

### Migrating off the built-in feature

`migration/` reads the built-in Custom Scripts feature, reports what moving off it would
do, and stages that content as inactive Projects. It exists in this shape for a reason
beyond the feature: **`migration/source.py` is the only module in the plugin that touches
the built-in implementation**, so the supported export service this needs from NetBox
Community is a change to one file rather than a search. Deriving that request from working
code, instead of guessing at it, is the same method that turned the execution-context ask
into a concrete one.

The tier reads and never imports. Dialect classification parses stored source with `ast`,
because the built-in `module_scripts` property executes a module to enumerate its classes
and an inventory must not run an operator's code to describe it. Staging adds no path into
storage: it creates Projects and declarations and then calls `ingestion`, which is why the
grouping rule had to fit the existing model rather than the reverse.

Two constraints shape grouping, and both come from the models rather than from preference.
The built-in feature stores a synchronized file under its base name, so `data_path` is the
only record of where it came from and the rule reads that path rather than inferring one.
And `ScriptProject.clean()` refuses two projects on one Data Source whose data paths
are ancestor and descendant, so a folder inside another script-holding folder joins it.
That is not a compromise: `ingest_data_source()` stages everything under `data_path`, so
the higher Project already holds the deeper file, and a migrated Project therefore holds
the helper modules beside a script, which the built-in feature could never synchronize.

**Staging declares only what would publish.** A built-in module the feature recorded no Script for,
and whose source defines no class that could publish one, is staged as a helper file with no
declaration. Both signals are needed and neither is a veto: the rows miss a class published through
inheritance or `script_order`, and the source shape misses one the built-in feature recorded under a
dialect this plugin does not name. Declaring a module that publishes nothing would make it the only
script file of its Project, which validation refuses, leaving a Project that can never be activated
and a migration that can never close.

**Staging activates nothing**, and every Project it creates takes the manual policy for that reason
alone: `RevisionValidationJob` activates a valid revision under any other one. The passes past the
fence do activate, rewrite and delete, and they take a permission of their own for it. Every pass is
re-runnable, so a write fence over the built-in feature is not a dependency of this tier. Every
symbol the tier reads is listed in
[`docs/development/netbox-internals.md`](./docs/development/netbox-internals.md), together
with the export service that would replace them.

**Past the fence, three rules carry the whole design and each one comes from the models rather than
from preference.** The order is forced: activation precedes the reference migration, because an
`EventRule.action_object_id` has to name a `NetBoxScript` row and activation is what creates those,
and Job history is repointed before anything is deleted, because `JobsMixin.jobs` is a
`GenericRelation` and `Script.module` is `CASCADE`. **The ordering is enforced on what is true, not
on what was recorded**: the three passes that name a Script gate on every mapped Project that still exists actually serving, because
activation records its step whether or not each Project could be put into service, and that record
is also what verification scopes itself by. **A step that left work a re-run could still do records
no completion**, or an operator who repairs a Project would meet a pass that returns at once and
does nothing, cleanup that blocks forever, and a run that can never close. **Every pass past the fence replays the map the cutover
froze**, never `mapping.build_map()`, because the collapse rule derives a key from every
script-holding folder on a source, so a module deleted since would change what its siblings group
into. `require_staged()` refuses a fence that recorded no map, since no later step could replay it. And **several references are left in place on purpose**, so they name the built-in
feature forever: a permission carrying constraints, an Event Rule action on a host with no registry, a
Job naming a module rather than a Script, and the history of a class that left its file, which NetBox
keeps as a non-executable Script row. The mapping excludes that last one outright, since it publishes
nothing and requiring a plugin row to resolve to it would block every later pass. Cleanup refuses to
delete anything holding one of the others and marks the refusal permanent, so the run still closes. A
reference the journal never captured appeared after the cutover, and is the only kind that counts as a
fault.

### Serialization

Every operation that touches stored content holds `storage.locks.project_lock()`,
keyed by the project's immutable `storage_key`. It is a PostgreSQL session-level
advisory lock on the two-integer keyspace, taken through the helper NetBox uses
for its own, and it does not hold a database transaction open across backend I/O.
The separation from core's keys is numeric rather than by arity, because core
takes two-integer pairs too, which `docs/development/netbox-internals.md`
records. Two deliberate exclusions: deletion takes no lock, because the
cleanup job rechecks references under it before reclaiming anything, and
validation takes it only for its row transitions, because the lease already owns
the long import span.

Activation is the domain operation and promotion is the storage primitive, and the
primitive cannot be called without a synchronizer. `storage.service.promote_revision()`
takes a **required** `on_promote` callback and invokes it inside the transaction that
moves the pointer, after both row locks and the identity recheck, so an active revision
and the `NetBoxScript` rows derived from it change together. `activation.activate_revision()`
is the only caller that passes a real one. A default would leave that bypass one call
away, which is why the parameter is required and why the rename from `activate_revision`
was worth roughly thirty test call sites: a missed one fails loudly instead of silently
skipping synchronization. The callback also runs on the already-active path, so
re-activating the current revision repairs rows, and because synchronization skips a row
that already matches, the repair writes nothing when nothing is wrong.

### Integration points with NetBox

The standard hooks every NetBox plugin uses. Fill in the per-plugin details
inline as the plugin grows.

- **PluginConfig**, `netbox_scripts/__init__.py` declares `name`,
  `label`, `verbose_name`, `description`, `version`, `author`,
  `author_email`, `base_url`, `min_version`, `max_version`. Add a
  `ready()` method that imports `signals` once you create that module.
- **Default model (ScriptProject)**, the scaffold ships a
  worked example object across every subsystem: model
  (`models/projects.py`),
  table (`tables/projects.py`),
  forms (`forms/<type>/projects.py`, by type: model_forms, bulk_edit, bulk_import, filtersets),
  filterset (`filtersets/projects.py`),
  seven views (`views/projects.py`),
  URL routes (`urls.py`), nav menu (`navigation.py`),
  search index (`search.py`),
  REST API (`api/views.py`, `api/serializers/projects.py`, `api/urls.py`),
  GraphQL (`graphql/{schema,types,filters,enums}.py`),
  and a per-area test suite covering model / API / view / table / form /
  filterset / GraphQL surfaces. Each test area mirrors that area's module layout:
  a flat area gets `tests/test_<area>.py`; a subpackage area gets
  `tests/<area>/test_projects.py` for the worked
  example, with related models grouped into topic leaves
  `tests/<area>/test_<topic>.py`, plus a `tests/<area>/__init__.py` anchor.
  The shared `tests/plugin_testing.py`
  mixins are always present.
  Run `manage.py makemigrations` on first render to generate the
  initial migration. Clear the `default_model_name` Copier answer
  (set it empty) and re-run `copier update` to remove the worked example.
- **Navigation**, `navigation.py` defines a `PluginMenu` with grouped
  `PluginMenuItem` entries and per-item permission checks.
- **URLs**, UI views are registered with `@register_model_view` and surfaced
  by `get_model_urls(APP_LABEL, '<model>')` in `urls.py`. The REST API uses a
  `NetBoxRouter` in `api/urls.py`.
- **Permissions**, Standard Django model permissions namespaced under
  `netbox_scripts.<perm>`.
- **Signals**, `signals.py` is the home for cross-model side-effects.
- **Search**, `search.py` registers `SearchIndex` subclasses for major models
  so they appear in NetBox's global search.
- **Cross-model UI**, `template_content.py` registers
  `PluginTemplateExtension` subclasses that extend NetBox's own (or another
  plugin's) detail pages.

## Commands

There is no Justfile / Makefile in this repo; commands are raw. Run them
inside a NetBox checkout that has this plugin installed with
`NETBOX_CONFIGURATION=configuration` exported and `$PWD/testing` on
`PYTHONPATH` (see the `## Development` section for setup).

| Command | What it does |
|---|---|
| `pip install -e '.[dev,test]'` (from this repo) | Install the plugin in editable mode with dev + test extras |
| `python netbox/manage.py test netbox_scripts.tests -v 2` | Run the plugin's test suite |
| `ruff check .` | Lint |
| `ruff format .` | Format |
| `pre-commit install` | Install the pre-commit hook into `.git/hooks` |
| `pre-commit run --all-files` | Run every default-stage hook against the whole tree |
| `pre-commit run --hook-stage manual check-manifest` | Run `check-manifest` (manual stage), exercise before tagging a release |
| `python scripts/check_netbox_internals.py --netbox <netbox>/netbox` | Resolve every NetBox internal the plugin depends on against that checkout, no database needed |
| `python netbox/manage.py makemigrations netbox_scripts` | Generate Django migrations after model changes |
| `python netbox/manage.py migrate` | Apply migrations |
| `python netbox/manage.py runserver` | Start NetBox locally with the plugin loaded |
| `mkdocs serve` | Preview the user docs |
| `python -m build` | Build sdist + wheel (matches the release workflow) |

## Development

NetBox plugins must run inside a NetBox checkout. The reproducible setup
mirrors what CI does (see `.github/workflows/test.yml`):

1. Clone NetBox alongside this repo
   (`git clone https://github.com/netbox-community/netbox.git`).
2. Point NetBox at the shipped `testing/configuration.py` via env vars:

   ```bash
   export PYTHONPATH="$PWD/testing:$PYTHONPATH"
   export NETBOX_CONFIGURATION=configuration
   ```

   The shipped config sets `PLUGINS = ['netbox_scripts']` and points
   at a local Postgres (netbox / netbox / netbox) plus Redis on default
   ports. NetBox's `manage.py` reads `NETBOX_CONFIGURATION` as a Python
   dotted module path and imports it against `sys.path`, so no symlink
   into the NetBox checkout is needed.
3. Install NetBox's requirements (`pip install -r netbox/requirements.txt`)
   and this plugin in editable mode (`pip install -e '.[dev,test]'`).
4. Provision Postgres (`netbox` / `netbox` / `netbox`) and Redis on
   localhost; the test config expects them on default ports.
5. Run migrations and start the dev server.

After model changes, generate a migration with NetBox's
`manage.py makemigrations netbox_scripts`, `related_name` changes are
no-op SQL but still need a migration for Django's state graph. Squash
periodically.

## Testing

- Tests use Django's `unittest.TestCase` (`django.test.TestCase`), **not**
  pytest. Suites live in `netbox_scripts/tests/`.
- Run via NetBox's test runner:

  ```bash
  python netbox/manage.py test netbox_scripts.tests -v 2
  ```

  The runner uses NetBox's settings and creates a real test database, so any
  code that touches the ORM, views, or APIs is exercised end-to-end.
- **Do not mock the database.** Use NetBox's test client and real fixtures;
  model-layer tests cover validators, constraints, and computed properties.
- **Query-count baselines.** NetBox 4.6+ view and REST API list tests assert
  each model's SQL query count against a baseline in
  `netbox_scripts/tests/query_counts.json`. The scaffold ships a baseline
  for the worked example, so a fresh render passes. After you add or change a
  model with a list view, regenerate it by running the suite once with
  `UPDATE_QUERY_COUNTS=1` serially (the recorder rejects `--parallel`), then
  commit the updated file:

  ```bash
  UPDATE_QUERY_COUNTS=1 python netbox/manage.py test netbox_scripts.tests
  ```

  **The baseline is a single file, but the CI matrix spans two NetBox refs, and a
  core change to query behaviour lands on them at different times.** The file
  therefore tracks the pinned stable ref, `netbox_test_max_ref`, and two rules
  follow from that:

  - **Regenerate against a checkout at that ref, never against `main` or
    `feature`.** `UPDATE_QUERY_COUNTS=1` rewrites every key it observes, so a run
    on a moving ref silently records counts the pinned legs will reject. The local
    development checkout floats across branches, so check which line it is on
    first.
  - **A count that changes only on `main` or `feature` is core's, not a
    regression.** The `feature` test leg is `continue-on-error` for exactly this
    reason. The baseline moves when the pinned ref moves, not before. Confirm
    the cause by running the same test against a pristine tree (`git archive
    HEAD` into a scratch directory, then point `PYTHONPATH` at it) before
    touching the file.

### Reporting test results

When reporting test results to a human reviewer, include:

- The exact command run.
- The working directory.
- Pass/fail count.
- The full failure output for any failing test.

Do not claim a test passed without running it.

## CI/CD

Three GitHub Actions workflows ship pre-wired under `.github/workflows/`:

- **`test.yml`**, PR / branch validation. Four jobs, three of them gated on
  a fast `lint` job running `pre-commit run --all-files`: a `test` matrix
  (Python versions x `[v4.7.0, feature]`), `test-branching` (one leg
  with NetBox Branching installed, the two branching test modules only), and
  `internals`, which resolves every symbol `docs/development/netbox-internals.md`
  lists against the `feature` ref. The `feature` test leg and the branching
  job report without blocking (`continue-on-error`). `internals` is the one
  blocking check on the moving ref: with no database, services or fixtures, a
  failure there is a crossing, the plugin skipped outside its version range,
  or a settings or setup change. Postgres and Redis service containers for
  the two test jobs. Triggers on pull requests and pushes to `main`.
- **`release.yml`**, Build + `twine check` + publish to NetBox Labs'
  internal CodeArtifact via the shared reusable workflow in
  `netboxlabs/internal-workflows`. Triggers on published GitHub
  releases. The IAM role ARN is built at runtime from the
  `AWS_ACCOUNT_ID` GitHub repository variable (see the
  [post-copy checklist](https://github.com/netboxlabs/netbox-plugin-scaffold/blob/main/docs/post-copy-checklist.md)
  for the full per-plugin setup).
- **`claude-review.yml`**, Claude AI PR review triggered by `@claude`
  mentions from authorized collaborators on a pull request. Set
  `ANTHROPIC_API_KEY` on the repository to enable. Review-assistance
  only, does not replace maintainer approval.

## Common Tasks

### Add a new model

1. Add the model class. In subpackage layout, add it to its topic module
   `models/<topic>.py` (group related models together; start a new topic
   module if none fits) and re-export each class from `models/__init__.py`
   (keep `__all__` alphabetised). In flat layout, append the class to
   `models.py`. Extend
   `PrimaryModel` for full features (tags,
   custom fields, comments) or `BaseModel` for line items / junction
   tables. Add `ContactsMixin` from `netbox.models.features` if contacts
   apply.
2. Add a `ChoiceSet` to `choices.py` for any new enum.
3. `python netbox/manage.py makemigrations netbox_scripts`.
4. Wire up the rest of the surface area. In subpackage layout, add the
   classes to each area's topic module (`filtersets/<topic>.py`,
   `tables/<topic>.py`, `api/serializers/<topic>.py`) and re-export each
   new class from the subpackage's `__init__.py`, keeping each `__all__`
   alphabetised. In flat layout, append the new classes directly to
   `filtersets.py`, `tables.py`, and `api/serializers.py`. Forms are
   always a subpackage; unlike the other areas, their `__init__.py`
   star-imports each by-type module, so adding a class needs no re-export
   edit: add each class to
   its by-type module (the edit form to `forms/model_forms`, the bulk-edit
   form to `forms/bulk_edit`, the bulk-import form to `forms/bulk_import`, the
   filter form to `forms/filtersets`), as a flat `<type>.py` in type_files
   layout or the per-topic `<type>/<topic>.py` file in type_subpackages
   layout; re-export is automatic via `forms/__init__.py`. In custom layout,
   follow the rendered per-area layout choices. In all cases, update
   `api/urls.py`, `urls.py`, `navigation.py`, and per-model templates under
   `templates/netbox_scripts/`.
5. Register a `SearchIndex` in `search.py` if the model should be globally
   searchable.
6. Add test classes for the new model, mirroring the `ScriptProject` classes the
   scaffold ships, for each surface (model layer, API, views, tables,
   forms, filtersets). In a flat area, append the class to the existing
   `tests/test_<area>.py`. In a subpackage area, add the test class to the
   topic's leaf `tests/<area>/test_<topic>.py` (the `tests/<area>/__init__.py`
   anchor already exists). Forms tests follow the forms shape: a flat
   `tests/test_forms.py` in type_files layout, or
   `tests/forms/test_<verbose>.py` in type_subpackages layout. Follow the
   same layout choice the module code uses for that area. Tests land in the
   same commit as the implementation.

### Add a REST API endpoint

1. Add the serializer. In subpackage layout, create
   `api/serializers/<area>.py` and re-export from
   `api/serializers/__init__.py`. In flat layout, append the class to
   `api/serializers.py`. In custom layout, follow the
   `serializers_layout` answer. Use `NetBoxModelSerializer` for
   `PrimaryModel`, `ValidatedModelSerializer` for `BaseModel`.
2. Add the viewset to `api/views.py` (extend `NetBoxModelViewSet`).
3. Register the route in `api/urls.py` via the `NetBoxRouter`.
4. Make sure a corresponding `FilterSet` exists (in subpackage layout
   `filtersets/<area>.py`, in flat layout `filtersets.py`, or per the
   `filtersets_layout` answer in custom layout) so `?field=` query
   params work; for FK filters, add an explicit
   `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)`
   rather than relying on `Meta.fields` to generate it.
5. Add an integration test class for the endpoint: append to
   `tests/test_api.py` (flat) or add `tests/api/test_<verbose>.py`
   (subpackage), matching the `serializers_layout` choice.

### Add a UI view

1. Add the view class. In subpackage layout (default), add it to the topic
   module `views/<topic>.py` and re-export from `views/__init__.py`.
   In flat layout, append the class directly to `views.py`. In either case,
   decorate with `@register_model_view(...)`. Use `netbox.views.generic.*View`
   base classes; do **not** add explicit URL patterns for object views.
2. Add the table. In subpackage layout, add it to the topic module
   `tables/<topic>.py` and re-export from `tables/__init__.py`. In flat
   layout, append the class to `tables.py`.
3. Add the template under `templates/netbox_scripts/`. Detail layouts
   use `netbox.ui.layout.SimpleLayout` with panel lists from `ui/panels.py`.
4. Wire URL prefixes in `urls.py` via `get_model_urls(APP_LABEL, '<model>')`.
5. Add the menu entry to `navigation.py` (with the right `permissions=[...]`).
6. For object-level buttons, add an `ObjectAction` subclass to
   `object_actions.py` and a button template under
   `templates/netbox_scripts/buttons/`.

### Bump the supported NetBox version

1. Update `min_version` / `max_version` in `netbox_scripts/__init__.py`.
2. Update `COMPATIBILITY.md`.
3. Update `netbox_test_min_ref` / `netbox_test_max_ref` (via `copier update`, or directly in the rendered `.github/workflows/test.yml`) to match the new supported floor / ceiling.
4. Run the suite locally against the new version.
5. Drop any shims that exist only for the now-unsupported NetBox versions.
6. Note any compatibility shims or breaking changes in `docs/releases.md`.

### Cut a release

1. Bump `version` in both `pyproject.toml` and `netbox_scripts/__init__.py`.
2. Update `docs/releases.md`.
3. Tag and publish a GitHub release. `release.yml` builds and publishes to
   the internal artifact store.

## Cloud and Enterprise compatibility (hard contract)

NetBox Cloud and NetBox Enterprise run this plugin as immutable, horizontally
scaled Kubernetes pods. Anything written outside a Django storage backend lands
on one pod and is gone from the next request, so a revision staged by a web pod
would be missing for the worker pod that has to execute it.

- **Persist bytes through a Django storage backend, never the local filesystem.**
  Project storage resolves its backend in `storage/config.py`, through the required
  `netbox_scripts` entry of `STORAGES`.
- **No authoritative per-pod state.** A disposable, manifest-verified runtime
  cache is supported, and it is the only layer that writes local executable
  files. Cache content is regenerated from the authoritative store and verified
  before import, never trusted because it exists.
- **No capability that only a management command can reach.** Neither platform can
  run one on demand, so anything a command is the sole route to does not exist for a
  Cloud or Enterprise operator. Put the work in a data migration or a `JobRunner` job.
  An *additive* command that duplicates a route already available to every platform is
  allowed, carries a `cloud-compat: ok` marker saying so, and must never become the
  only way to reach a capability. Its own arguments may still differ, so
  `runcustomscript --user` attributes a run to another account where REST always runs as
  the token's user. `management/commands/runcustomscript.py` is the one
  such command and the pattern to follow.
- **No in-process schedulers or background threads.** A pod is killed without
  warning, so long-running work belongs in a job. Remote storage I/O stays out
  of the committing process for the same reason, which is why deletion cleanup
  is enqueued as a job. The cleanup Job commits inside the deleting transaction,
  which binds these models to the default database, enforced across staging,
  activation, and the deletion signal, and the Job repeats the routing safety
  check when it runs.
- **No per-pod application caches.** Redis is the shared cache on both
  platforms. The runtime cache above is the deliberate exception, because
  Python imports need a real directory tree.

The contract is enforced twice. The backend contract tests in
`netbox_scripts/tests/storage/test_backend_contract.py` drive the
storage lifecycle against backends without filesystem paths or directory
semantics, proving the behavior. The AST checker `scripts/check_cloud_compat.py`
(a pre-commit hook, so the CI lint job runs it) polices where local writes live:
it flags filesystem calls, per-pod state, threads, shell-outs, and management
commands anywhere in the package, and only a statement carrying the
`cloud-compat: ok` marker with a reason is exempt. The runtime cache tier is the
one place those markers belong.

Check this before designing anything that persists bytes.

## Conventions and Patterns

- **Plugin code stays in the plugin package.** Do not monkey-patch NetBox.
- **Use NetBox's mixins** where they exist (`PrimaryModel`, `BaseModel`,
  `ContactsMixin`, `NetBoxModelSerializer`, `NetBoxModelFilterSet`,
  `BaseFilterSet`) rather than re-implementing the same behaviour.
- **All UI views use `@register_model_view`** from `utilities.views`.
- **URL segments never repeat the plugin name.** `PluginConfig.base_url` already
  scopes every route, so a model's segment is its own plural noun: `projects/`,
  `script-files/`, giving `/api/plugins/netbox-scripts/script-files/`, never
  `netbox-script-files/`. Segments are the only thing this affects, since
  reverse names come from the model (DRF derives the router basename from
  `queryset.model`, and `register_model_view` / `get_model_urls` name UI routes),
  so renaming a segment changes no `reverse()` call, table, or menu item.
- **Detail layouts** use `netbox.ui.layout.SimpleLayout` with panel lists
  from `ui/panels.py`.
- **Object-level buttons** are `ObjectAction` subclasses in
  `object_actions.py`, paired with templates in
  `templates/netbox_scripts/buttons/`.
- **FK filters** must declare an explicit
  `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)` in the
  filterset; do not rely on `Meta.fields` to generate `_id` variants. Filter
  form `model` attribute must match the filterset's model, not the parent
  model.
- **`db_collation="natural_sort"`** on code / identifier fields for
  human-friendly ordering.
- **Cross-model side-effects** live in `signals.py` and are wired in
  `AppConfig.ready()`.
- **Search registration** lives in `search.py`; cross-model UI extensions
  live in `template_content.py`.
- **Permissions namespaced** under `netbox_scripts.<perm>`. Used by
  `navigation.py` menu items and view base classes.
- **`Meta.permissions` declares the BARE action**, `('run', ...)` and not
  `('run_netboxscript', ...)`, matching `core.DataSource`'s `('sync', ...)` and
  `dcim.Device`'s `('render_config', ...)`. NetBox registers each codename verbatim as a
  tickable action in the permission picker, and the backend composes
  `f'{app_label}.{action}_{model_name}'` from whatever an administrator ticked, so a codename
  carrying the model name grants `run_netboxscript_netboxscript`, which nothing checks. Call
  sites are unaffected either way: they ask for the composed
  `netbox_scripts.run_netboxscript`, which `get_permission_for_model()` builds from the
  action, never from the codename. One consequence to know rather than discover: the bare
  codename also produces a bare Django permission row, and `RemoteUserBackend` sits ahead of
  `ObjectPermissionBackend` in `AUTHENTICATION_BACKENDS` and does read `auth_permission`, so a
  plain Django grant of one of these five no longer reaches the view that checks the composed
  string. Core is the same, `core.DataSource` declares `('sync', ...)` while its sync view asks
  for `core.sync_datasource`, and `docs/permissions.md` already documents Object Permissions as
  the route. `tests/test_permissions.py` pins both the row and the registry the picker reads,
  because only the second would have caught the codename being wrong.
- **Migrations.** Prefer a single squashed `0001_initial.py` until the
  schema settles. Data migrations use `RunPython` with `apps.get_model(...)`
  and `get_or_create`, `ContentType` rows may not exist yet at migration
  time.
- **Migration dependencies must exist at `PluginConfig.min_version`.**
  `makemigrations` pins whatever the local NetBox checkout has, which can name a
  migration the declared minimum does not ship. After every `makemigrations` run,
  re-pin the deps to these, which is what `0001_initial.py` carries:
  `('core', '0024_job_notifications')`,
  `('extras', '0138_customfieldchoiceset_choice_colors')`,
  `('users', '0016_default_ordering_indexes')`.
  **These are the v4.6.0 heads and they stay, even though the floor is now 4.7.0.**
  Migration graphs are append-only, so a 4.7 install has them applied and they still
  resolve, while a 4.7 head could still be renumbered before the release is final.
  Do not "correct" them to the current line.
  The same floor rule applies to inherited field definitions. `OwnerMixin.owner`
  carries `related_name='+'` on the 4.7 line, which `0001_initial.py` now encodes
  on all three PrimaryModel subclasses. Reconcile to the declared floor rather than accepting a
  standing `makemigrations --check` diff, because tolerated drift is how a real
  schema change gets missed.
- **Project identity invariants.** `key` and `source_type` are immutable
  after creation (enforced in `clean()`, mirrored by disabled form fields);
  `storage_key` never changes (enforced in `save()`); `data_path` is stored
  in canonical form via `validators.normalize_data_path()` and must be
  non-empty for `data_source` projects (backed by the
  `enforce_source_ownership` check constraint). Code using
  `QuerySet.update()` bypasses normalization and must supply canonical
  values itself.
- **GraphQL choice fields** follow NetBox Community's convention: typed enums (from
  `graphql/enums.py`) appear on filter inputs only, and object types expose raw
  choice values as strings.
- **Docstrings carry the contract, not the reasoning.** One line by default. It
  earns more lines only for behaviour a caller has to branch on, never for
  rationale: that a call does not raise on bad input, that it deduplicates, what
  it returns, what it raises. If a sentence explains *why* the code is written
  the way it is, it does not belong in a docstring. Three rules follow, and the
  first two name where the displaced prose goes instead:
  - Rationale about one specific line is an **inline comment at that line**,
    not a paragraph in the docstring. `storage/paths.py` (the `MAX_PATH_BYTES`
    budget) and `runtime/cache.py` (the `mkdir` mode and bytecode traps) are
    the reference examples.
  - Cross-cutting rationale (lock discipline, fail-closed policy, the
    database-alias contract) belongs in `docs/` and the Architecture section
    above, stated once and referenced. Do not restate it per module or per
    function.
  - A docstring that only rewords its own identifier and base class states
    nothing, but `D101` is enforced, so improve it rather than removing it.
    `class FooListView(generic.ObjectListView)` earns a line naming what it
    lists and any narrowing it applies, not "List view for Foo".

  No section-banner comments (`# Validation`, `# Helpers`). Split the module
  instead if it needs signposting. The `cloud-compat: ok` markers are not prose
  and this entry does not apply to them, `scripts/check_cloud_compat.py`
  requires their exact form.
- **Linting.** ruff config lives under `[tool.ruff*]` in `pyproject.toml`;
  no separate `ruff.toml`. Line length 120, single quotes, LF line endings,
  `preview = true`. See `pyproject.toml` for the full rule set.
- **Pre-commit.** Hook wiring lives in `.pre-commit-config.yaml`; tool
  configuration lives in `pyproject.toml` where the hook supports it
  (yamllint is the documented exception).
- **Backwards compatibility for one minor.** Breaking API changes get at
  least one minor's worth of deprecation warning before removal.

## Troubleshooting

- **Tests fail with "ContentType matching query does not exist".** Data
  migrations must `get_or_create` `ContentType` rows; the `post_migrate`
  signal that populates them has not fired yet during a fresh `migrate`.

## References

- Plugin README: [`README.md`](./README.md).
- Compatibility matrix: [`COMPATIBILITY.md`](./COMPATIBILITY.md).
- Security policy: [`SECURITY.md`](./SECURITY.md).
- License: [`LICENSE.md`](./LICENSE.md).
- User docs (mkdocs): [`docs/`](./docs/).
- NetBox plugin docs: <https://netboxlabs.com/docs/netbox/plugins/>.
- Repository: <https://github.com/netboxlabs/netbox-custom-scripts>.
