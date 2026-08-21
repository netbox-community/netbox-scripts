# AGENTS.md, netbox-custom-scripts

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

`netbox-custom-scripts` is a NetBox plugin: Custom Scripts for NetBox It is owned by
NetBox Labs and runs inside NetBox as a Django app (`netbox_custom_scripts`).
The supported NetBox version range is in `COMPATIBILITY.md`
(4.7.0 to 4.7.99 at scaffold time).

Version pins live in two places:

- `pyproject.toml`, Python, build, and dependency pins.
- `netbox_custom_scripts/__init__.py`, `PluginConfig.min_version` /
  `PluginConfig.max_version` for the NetBox host app.

Defer all version pins to those files; do not duplicate them elsewhere.

## Tech Stack

- Python 3.12+ (defer to `pyproject.toml` for the exact pin).
- NetBox (host app, min/max in `netbox_custom_scripts/__init__.py`).
- Django + Django REST Framework (NetBox's foundations).
- Django's built-in test runner (this plugin does **not** use pytest, the
  suite is `django.test.TestCase`-based and runs via `manage.py test`).
- ruff for lint + format (config in `pyproject.toml` under `[tool.ruff*]`;
  no separate `ruff.toml` file).
- pre-commit for local quality gates (config in `.pre-commit-config.yaml`).
- mkdocs + mkdocs-material for user-facing docs.
- NetBox's `manage.py` for running the plugin during local dev.

## Repository Map

The scaffold ships a working `CustomScriptProject` model across every
subsystem (model, table, forms, filterset, views, urls, navigation,
search, REST API, GraphQL, test) as a worked example. Entries marked
`[CustomScriptProject]` are the shipped first-class object; entries marked
`[add as needed]` are conventional NetBox plugin modules that you add
when domain content calls for them.

```text
.
├── netbox_custom_scripts/            , The Django app.
│   ├── __init__.py                , [stub] PluginConfig (name, version, base_url, min/max NetBox).
│   ├── urls.py                    , urlpatterns for 'modules/' + 'projects/' + 'scripts/' + detail-only 'revisions/<int:pk>/' via get_model_urls, sorted. Segments never repeat the base_url. The eight 'migration/' routes are explicit paths, because the passes act on the built-in feature and have no model of their own. The run's own detail route goes through get_model_urls like any other.
│   ├── navigation.py              , PluginMenu 'Custom Scripts' with a Projects group and a Scripts group, each labelled like every sibling plugin. Scripts get no add button and Modules get no nav item: a declaration is a Project setting. Migration sits in the Projects group, because a pass produces Projects.
│   ├── api/
│   │   ├── __init__.py            , [stub]
│   │   ├── urls.py                , router.register for 'modules' + 'projects' + 'scripts'.
│   │   ├── views.py               , RunScriptPermissions + CustomScriptModuleViewSet + CustomScriptProjectViewSet with its GET/PUT `entrypoints` action and its POST `upload` action (UploadSourcePermissions resolves POST to change, since the project exists and its source moves, and initial() re-narrows the queryset for the same reason. The Module add permission is checked in the action, matching the Add Script page, because an upload declares an entrypoint) + read-only CustomScriptProjectRevisionViewSet (NetBoxReadOnlyModelViewSet, so no write route is registered) + update-only CustomScriptViewSet (http_method_names drops POST and DELETE, since composing the mixins instead would drop NetBoxModelViewSet.update() and with it the changelog snapshot and the If-Match check) with its POST `run` action. Each viewset select_relates the revision its serializer nests. The run action carries its own permission class and http_method_names, because both defaults key off the HTTP method and would resolve POST to add, which no caller of a derived model holds. initial() narrows it by the run action for the same reason.
│   │   └── serializers/
│   │       ├── __init__.py        , Re-exports CustomScriptModuleSerializer, CustomScriptProjectRevisionSerializer, CustomScriptProjectSerializer, CustomScriptProjectUploadSerializer, CustomScriptRunInputSerializer, CustomScriptSerializer.
│   │       ├── revision.py    , CustomScriptProjectRevisionSerializer: read-only, and also the serializer event serialization resolves by model name. Omits the manifest, the entrypoint snapshot and the validation lease fields.
│   │       ├── script.py      , CustomScriptSerializer: derived fields in read_only_fields, importable as api.serializers.CustomScriptSerializer for event serialization.
│   │       ├── project.py     , [CustomScriptProject] CustomScriptProjectSerializer.
│       │       ├── upload.py      , CustomScriptProjectUploadSerializer: the upload envelope. One file plus confirm_replace, and no destination field, ever. The path is the basename.
│   │       ├── run.py         , CustomScriptRunInputSerializer: the run envelope. Variable values nest under `data`, so a variable cannot collide with an execution parameter. Refuses a past schedule and one the script class forbids.
│   │       └── module.py      , CustomScriptModuleSerializer: nested project, discovery fields read-only, nested read-only revision.
│   ├── filtersets/
│   │   ├── __init__.py            , Re-exports CustomScriptFilterSet, CustomScriptModuleFilterSet, CustomScriptProjectFilterSet, CustomScriptProjectRevisionFilterSet.
│   │   ├── project.py             , [CustomScriptProject] CustomScriptProjectFilterSet with custom search().
│   │   ├── revision.py            , CustomScriptProjectRevisionFilterSet(ChangeLoggedModelFilterSet): project by id + key, status, both digests.
│   │   ├── module.py              , CustomScriptModuleFilterSet: project by id + key, discovery filters, custom search().
│   │   └── script.py              , CustomScriptFilterSet: project by id + key, explicit MultiValueCharFilter for the TextField description, explicit MultipleChoiceFilter for the notification override, the other two overrides generated, metadata unfiltered.
│   ├── forms/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports each by-type subpackage.
│   │   ├── model_forms/project.py   , [CustomScriptProject] CustomScriptProjectEditForm + CustomScriptProjectEntrypointsForm (reconciles the selection onto enabled, then enqueues ProjectEntrypointRefreshJob when it moved).
│   │   ├── model_forms/module.py    , CustomScriptModuleEditForm (project + source_path frozen, so disabled on edit).
│   │   ├── bulk_edit/project.py     , [CustomScriptProject] CustomScriptProjectBulkEditForm.
│   │   ├── bulk_import/project.py   , [CustomScriptProject] CustomScriptProjectBulkImportForm.
│   │   ├── model_forms/script.py    , CustomScriptEditForm: writable set is enabled, the three execution overrides, comments, owner, tags and custom fields. A plain NetBox model form, because every derived column is editable=False and therefore already out of it.
│   │   ├── bulk_edit/script.py      , CustomScriptBulkEditForm: enabled only, description removed declaratively since BulkEditView setattr ignores editable=False.
│   │   ├── filtersets/project.py    , [CustomScriptProject] CustomScriptProjectFilterForm.
│   │   ├── filtersets/module.py     , CustomScriptModuleFilterForm.
│   │   └── filtersets/script.py     , CustomScriptFilterForm.
│   ├── migrations/                , [CustomScriptProject] 0001_initial.py; regenerate on schema change and keep the pinned deps (see Conventions).
│   ├── models/
│   │   ├── __init__.py            , Re-exports CustomScript, CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision, MigrationRun.
│   │   ├── project.py             , CustomScriptProject(PrimaryModel), whose Meta.permissions carries activate / migrate / reconcile beside the four standard actions, with identity/ownership invariants and entrypoint_candidates / declarable_entrypoints / select_entrypoints + CustomScriptProjectRevision (immutable content fields, entrypoint snapshot in identity, status lifecycle, validation lease fields).
│   │   ├── module.py              , CustomScriptModule(PrimaryModel): declared entrypoints, canonical importable source_path frozen with project after creation, sibling rejection by letter case and by module name, system-managed discovery fields.
│   │   ├── script.py              , CustomScript(JobsMixin, PrimaryModel): one published Script class, identity project + module_path + class_name, description overrides the abstract base as an unbounded TextField, enabled (admin) separate from is_retired (sync). Three nullable execution-override columns sit beside enabled, empty meaning inherit, and the accessors resolve override then class then built-in default. scheduling_enabled takes no override, it is the author's safety claim.
│   │   └── migration.py           , MigrationRun(ChangeLoggedModel): one attempt at moving off the built-in feature. Migration infrastructure rather than a domain model, so no REST, GraphQL or list view. State moves forward one step only, at most one run is open, and the journal is what every cutover step replays from. complete_step() and recorded_counts() are the one home of the resume guard every caller needs.
│   ├── tables/
│   │   ├── __init__.py            , Re-exports CustomScriptModuleTable, CustomScriptProjectFileTable, CustomScriptProjectRevisionTable, CustomScriptProjectTable, CustomScriptTable.
│   │   ├── project.py                 , [CustomScriptProject] CustomScriptProjectTable(PrimaryModelTable) + CustomScriptProjectRevisionTable(BaseTable), the history table with no list view. Its ActionsColumn carries only extra_buttons and needs exempt_columns to render, since BaseTable hides unselected columns. + CustomScriptProjectFileTable, the Files tab fed the manifest's dictionaries rather than a queryset.
│   │   ├── script.py              , CustomScriptTable + CustomScriptLogTable, the run log fed a list of dictionaries rather than a queryset.
│   │   └── module.py              , CustomScriptModuleTable: source_path is the linked column, revision column unlinked.
│   ├── tests/                     , Each area mirrors its module layout (flat file or subpackage).
│   │   ├── __init__.py            , [stub] Test discovery anchor.
│   │   ├── plugin_testing.py      , [shared] Plugin-aware view/API test mixins (always rendered). PrimaryObjectViewTestCase + NestedObjectViewTestCase + DerivedObjectViewTestCase, the last for models whose rows are derived, so no create, delete, or import. Also the one home of ChangeLoggedFilterSetTestMixin, which the three filterset suites import from here rather than from the host.
│   │   ├── models/__init__.py     , [CustomScriptProject] Test package anchor.
│   │   ├── models/test_project.py , [CustomScriptProject] CustomScriptProjectTestCase: create, str, absolute_url, data_path canonicalization, immutability + constraint invariants.
│   │   ├── api/__init__.py        , [CustomScriptProject] Test package anchor.
│   │   ├── api/test_project.py , [CustomScriptProject] CustomScriptProjectAPIViewTestCase(PluginAPIViewTestCases.APIViewTestCase).
│   │   ├── views/__init__.py      , [CustomScriptProject] Test package anchor.
│   │   ├── views/test_project.py , [CustomScriptProject] CustomScriptProjectTestCase(PluginTestCases.PrimaryObjectViewTestCase).
│   │   ├── tables/__init__.py     , [CustomScriptProject] Test package anchor.
│   │   ├── tables/test_project.py , [CustomScriptProject] CustomScriptProjectTableTestCase(TableTestCases.StandardTableTestCase).
│   │   ├── forms/__init__.py      , [CustomScriptProject] Test package anchor.
│   │   ├── forms/test_project.py , [CustomScriptProject] EditForm / FilterForm / BulkImportForm test cases.
│   │   ├── filtersets/__init__.py , [CustomScriptProject] Test package anchor.
│   │   ├── filtersets/test_project.py , [CustomScriptProject] CustomScriptProjectFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests).
│   │   ├── graphql/__init__.py    , [CustomScriptProject] Test package anchor.
│   │   ├── graphql/test_project.py , [CustomScriptProject] CustomScriptProjectGraphQLTestCase: enum members match the ChoiceSets.
│   │   ├── models/test_module.py  , CustomScriptModule model invariants.
│   │   ├── models/test_script.py  , CustomScript identity, retirement, cascade + is_executable.
│   │   ├── api/test_script.py     , CustomScriptSerializer route reversal, event serialization, patchable enabled, ignored derived fields, refused create/delete.
│   │   ├── api/test_revision.py   , Revision serializer resolution by model name, rendering without a route, REST delete of an activated project.
│   │   ├── api/test_run.py        , The scripts/<id>/run/ contract: the envelope, the Job response, the refusals, and that neither the add permission nor an object constraint can be bypassed.
│   │   ├── views/test_script.py   , CustomScriptViewSetTestCase(PluginTestCases.DerivedObjectViewTestCase) + detail view, changelog rendering, and the absent create/delete routes.
│   │   ├── views/test_run.py      , The run page, the run permission gate, the queued payload, the result page and its level threshold, plus one end-to-end submit-and-execute.
│   │   ├── views/test_actions.py  , Static guard: every list, detail and row action is checked against the registered routes, because ActionsMixin and ActionsColumn filter by permission alone.
│   │   ├── views/test_files.py    , The Files tab: manifest rows, the entrypoint marker, the missing-path annotation, the empty state, and the view gate.
│   │   ├── views/test_migration.py , The Migration page and its seven enqueue routes: both permission gates, every enqueue, the queued-pass refusals, and which button each state renders.
│   │   ├── views/test_migration_run.py , One migration's detail page.
│   │   ├── models/test_migration_run.py , MigrationRun: the state machine, the single-open-run rule, and the journal helpers.
│   │   ├── tables/test_script.py  , CustomScriptTableTestCase(TableTestCases.StandardTableTestCase).
│   │   ├── filtersets/test_script.py , CustomScriptFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests), metadata in ignore_fields.
│   │   ├── forms/test_script.py   , Edit + bulk edit forms: the writable set, and that a stale save cannot revert a derived field.
│   │   ├── graphql/test_script.py , CustomScriptGraphQLTestCase: last_seen_revision absent from the type.
│   │   ├── views/test_revision.py , Activate/Deactivate buttons: round trip, refusals, permissions, and which button each status renders.
│   │   ├── views/test_reconcile.py , The Reconcile Source action: the confirmation, the enqueue, the permission gate, and which source type renders the button.
│   │   ├── api/test_module.py     , CustomScriptModuleAPIViewTestCase: read-only discovery fields, path canonicalization + refusals.
│   │   ├── views/test_module.py   , CustomScriptModuleTestCase(PluginTestCases.NestedObjectViewTestCase).
│   │   ├── tables/test_module.py  , CustomScriptModuleTableTestCase(TableTestCases.StandardTableTestCase).
│   │   ├── forms/test_module.py   , Edit / Filter form test cases.
│   │   ├── forms/test_entrypoints.py , Selection reconciles onto enabled, nested paths, missing declared paths.
│   │   ├── api/test_entrypoints.py , The projects/<id>/entrypoints/ GET + PUT contract.
│   │   ├── models/test_entrypoint_candidates.py , Candidate enumeration from DataFile and from the newest manifest.
│   │   ├── filtersets/test_module.py , CustomScriptModuleFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests), every field filterable.
│   │   ├── graphql/test_module.py , CustomScriptModuleGraphQLTestCase: discovery enum matches the ChoiceSet, the revision relation resolves.
│   │   ├── graphql/test_revision.py , CustomScriptProjectRevisionGraphQLTestCase: status enum matches the ChoiceSet, stored documents and lease fields stay off the type.
│   │   ├── storage/               , Storage tier suites: config, paths, manifest, entrypoints, store, service, signals, jobs, branching, backend contract, concurrency. test_concurrency.py is the suite's only TransactionTestCase, so it is the only one that really commits and therefore the only one whose on_commit callbacks enqueue live RQ jobs. It drains the queue in cleanup so the suite leaves nothing behind even in the isolated database.
│   │   ├── runtime/               , Runtime tier suites: test_cache.py, test_naming.py, test_loader.py, test_discovery.py, test_introspection.py, test_resolution.py.
│   │   ├── scripts/               , Authoring API suites: test_base.py, test_variables.py, test_exports.py.
│   │   ├── compat/                , Legacy compatibility suites: test_imports.py (compat_import per statement form, both sides of the transition, no database) and test_dialects.py (every form end to end out of one validation pass).
│   │   ├── migration/             , Migration tier suites: test_source.py (the read seam against real built-in rows), test_dialects.py (classification per import statement form, no database), test_plan.py (grouping including the nested-folder collapse, the findings, and the inventory job), test_staging.py (both source types end to end, idempotence, and that no staged revision activates), test_mapping.py, test_cutover.py, test_activation.py, test_references.py, test_history.py, test_cleanup.py (the per-module refusals, which are the safety net the whole ordering exists for), test_verification.py. LegacySourceMixin in test_staging.py is the shared built-in fixture and the mixins chain off it, so a suite reuses the fixture without re-running the borrowed suite's tests. It points the 'scripts' storage alias at a real temporary directory, never in-memory, because ManagedFile.storage is a fresh instance while the loader reads the cached one.
│   │   ├── test_execution.py      , run_script() suites (commit and dry-run, both abort classes, failure logging, request-processor selection and isolation, the current-request restore) plus CustomScriptJob suites (pinning, the administrative recheck, resolution failures, run-record sanitization).
│   │   ├── test_validation.py     , Validation service + RevisionValidationJob suites (claim/reclaim, fencing, classification, sanitization, Module persistence).
│   │   ├── test_activation.py     , SynchronizeScriptsTestCase (upsert/retire/no-op-write semantics) + ActivateRevisionTestCase + PromotionCallbackTestCase (required callback, savepoint depth, rollback).
│   │   ├── test_ingestion.py      , Ingestion ordering and failure modes for both callers, plus UploadToActiveTestCase and DataSourceToActiveTestCase: each slice end to end against real validation.
│   │   ├── test_entrypoint_refresh.py , The selection-change path: the enqueue, the Job body, the form's changed-only rule, and the activation policy end to end.
│   │   ├── test_permissions.py    , The source-management separation: change alone cannot activate or reconcile, each own action can, and an object constraint narrows both projects and their revisions.
│   │   ├── test_event_rules.py    , The `netbox_custom_scripts.run` action: registration, the refusals validate() makes and the ones it leaves to dispatch, the event payload, and import resolution by project key. Skipped entirely below the 4.7 line through an importlib.util.find_spec probe, which is the only guard the feature needs. One class reads the queued task, because enqueue_run keeps script input off the Job row so that is the only place action_data can be seen.
│   │   ├── test_event_sources.py  , The plugin's models as Event Rule sources: all four qualify, a rule saves against one, the webhook body carries identity and no stored document, and a matching rule reaches the queue. The queue is isolated in testing/configuration.py rather than per class, so emptying here only separates one test from the next. Never clear with RQQueueTestMixin, which uses a server-wide flushall(). Dispatch needs captureOnCommitCallbacks, since django_rq defers an enqueue to on_commit and a TestCase never commits.
│   │   ├── test_management.py     , The runcustomscript command: what it resolves, what it refuses, that a committed run is change logged against the named user, and that a failure raised before the script is reached still reports why.
│   │   └── test_reconciliation.py , The post_sync receiver (which projects, and that it never fails a sync) plus ProjectReconciliationJob, including the reverted-directory activation.
│   ├── views/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports every view class, `__all__` alphabetised.
│   │   ├── migration.py            , The Migration page, the run's detail view, and the seven enqueue views, on TWO gates. BaseMigrationView takes add_customscriptproject for the page, the inventory, staging and verification, because creating Projects is all those authorize. DestructiveMigrationView takes migrate_customscriptproject for the cutover, activation, repoint and cleanup, because closing and deleting rows of the built-in feature is not a form of creating a Project. Staging, the cutover and cleanup confirm first and refuse while a pass of their own class is queued, the inventory and verification do neither, because they write nothing.
│   │   ├── project.py                  , [CustomScriptProject] List/Detail/Edit/Delete/BulkEdit/BulkDelete/BulkImport views, the Entrypoints tab, the read-only Files tab, the Revisions history tab, Upload (create) + Add Script (detail) upload views, and the Activate and Reconcile confirmation views. Reconcile narrows its queryset to Data Source-backed projects, so the route does not apply to an uploaded one.
│   │   ├── module.py               , List/Detail/Edit/Delete/BulkDelete views. No bulk edit or bulk import: selection happens on the Project.
│   │   ├── script.py               , List/Detail/Edit/BulkEdit views plus Run (GET builds the class's own form out of the active revision, POST enqueues) and Result (one run's log, read out of the Job). No add, delete, bulk delete or bulk import: rows are derived from an activated revision, and retirement replaces deletion. The Jobs tab needs no view, JobsMixin registers one. Run declares a ViewTab gated on the run permission, so every view of the script offers it, and builds its form through execution.load_script_class(), which the REST run action shares. Result serves its body as a partial to an htmx poll, so a run that has not reached a terminal state refreshes itself, at a slower rate while it is only scheduled.
│   │   └── revision.py             , Detail view for one revision, plus Activate + Deactivate, GET confirms and POST performs. Gated on the PROJECT's activate permission, with the revision queryset narrowed to permitted projects. The tab links here rather than posting: its table is inside the bulk-action form, so a nested form would submit the outer one.
│   ├── ui/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports CustomScriptProjectPanel + CustomScriptProjectSourcePanel.
│   │   └── panels.py              , [CustomScriptProject] CustomScriptProjectPanel (left) + CustomScriptProjectSourcePanel and CustomScriptProjectStatePanel (right) for the detail view layout, plus CustomScriptPanel and CustomScriptStatePanel, the two Module panels, and the two Revision panels.
│   ├── search.py                  , [CustomScriptProject] CustomScriptIndex + CustomScriptModuleIndex + CustomScriptProjectIndex, each registered via @register_search.
│   ├── graphql/
│   │   ├── __init__.py            , [CustomScriptProject] Exports schema = [Query].
│   │   ├── schema.py              , [CustomScriptProject] @strawberry.type(name='Query') with custom_script_project / custom_script_project_list fields.
│   │   ├── types.py               , [CustomScriptProject] CustomScriptProjectType(PrimaryObjectType); choice fields expose raw string values.
│   │   ├── filters.py             , [CustomScriptProject] CustomScriptProjectFilter(PrimaryModelFilter) with enum-typed choice filters; no storage_key filter.
│   │   └── enums.py               , [CustomScriptProject] ProjectSourceTypeEnum + ActivationPolicyEnum via strawberry.enum(ChoiceSet.as_enum()).
│   ├── storage/
│   │   ├── config.py              , Resolves the required STORAGES['netbox_custom_scripts'] backend and limit settings.
│   │   ├── paths.py               , Canonical source paths, the case-insensitive comparison, compiled-artifact refusal, storage keys.
│   │   ├── manifest.py            , Manifest build/validate pair, content digests, per-node letter-case collision rejection.
│   │   ├── entrypoints.py         , Entrypoint snapshot build/validate pair, the return-trip trust boundary.
│   │   ├── store.py               , Verified writes and reads against the backend, copy_verified bounded-read primitive, read_verified / read_revision_tree in-memory reads.
│   │   ├── locks.py               , project_lock(): the per-project advisory lock every content operation holds, and the one home of the key derivation.
│   │   ├── service.py             , stage_revision / refresh_revision_entrypoints / promote_revision (mandatory on_promote callback) + the database-alias contract.
│   │   └── exceptions.py          , Storage error taxonomy.
│   ├── runtime/
│   │   ├── cache.py               , Manifest-verified local materialization of revision trees, the one sanctioned local-write tier.
│   │   ├── loader.py              , Private-namespace package loader: import sessions, failure sweep, unload.
│   │   ├── naming.py              , Private module names + the entrypoint dotted-name adapter.
│   │   ├── discovery.py           , discover_scripts(): publication rules, script_order, identity + logger markers.
│   │   ├── introspection.py       , describe_script / validate_discovered_scripts: forces run-form construction, the JSON-safe published-script record.
│   │   ├── resolution.py          , resolve_script_class(): a stored identity back to a live class through the snapshot's entrypoint provenance. Opens no import session and never unloads, the caller runs what it returns.
│   │   └── exceptions.py          , Runtime error taxonomy (cache, module path, import, discovery, resolution).
│   ├── scripts/                   , Authoring API: base.py (BaseScript/Script, whose scheduling_offered ANDs the class's scheduling_enabled with the caller-set scheduling_permitted, so the fieldsets and the form cannot disagree, and which carries the two run-context attributes the runner binds: request, and event for a run an Event Rule drove), variables.py, forms.py (ScriptForm carries the four execution parameters, and drops the two scheduling fields when scheduling is not offered), logging.py, exceptions.py.
│   ├── compat/
│   │   ├── __init__.py            , install() + the name-scoped finder + wrap_loader + compat_import + the extras stand-in + the host probe. One rule: inside revision code the name `extras` resolves through the stand-in.
│   │   └── legacy.py              , Report, carrying the marker discovery refuses, so an unsupported dialect produces a message rather than a Run button that raises.
│   ├── migration/
│   │   ├── __init__.py            , Tier docstring only, like storage/ and runtime/. No re-exports.
│   │   ├── source.py              , The ONLY module that reads the built-in Custom Scripts feature, so a supported export service is a change to one file. Read-only, returns frozen dataclasses and plain counts. Filters to the SCRIPTS root: the proxy manager admits reports as well, and a report's bytes live under REPORTS_ROOT while every read here goes through the scripts backend, so one could never be read. Reports are outside this migration entirely and legacy_report_count() is what makes that visible. Opens `file_path` rather than `full_path`, and resolves both content types with for_concrete_model=False because a Job and an Event Rule record the proxy.
│   │   ├── dialects.py            , ast-based classification of stored source into native / legacy_import / report_style / unparsable. Imports nothing: an inventory must not execute an operator's code to classify it. Report shape outranks a legacy import, because the two cost an author different work.
│   │   ├── plan.py                , The grouping rule and the inventory report, pure functions over seam output. One Project per folder that holds scripts, and _collapse() merges a folder into the shallowest script-holding folder containing it, because the model refuses two overlapping data paths on one source and the higher Project's tree already holds the deeper file.
│   │   ├── mapping.py             , Which plugin Custom Script one built-in Script becomes, derived rather than stored. module_path is the DOTTED name off cls.__module__, so the identity composes the collapse rule with source_path_to_dotted_name().
│   │   ├── staging.py             , Creates the proposed Projects and delegates to ingestion. Every Project takes the MANUAL policy, since validation would otherwise activate under any other one. Validates rather than get_or_create's, so a path overlapping a hand-made project is refused instead of written.
│   │   ├── cutover.py             , The irreversible step: capture every reference the later passes replay, plus the plugin map they all resolve through, then close what a plugin can. Captures once, because a second capture would read the closed state back as the original. Also activate_staged(), which comes after the fence because an Event Rule's action object has to name a CustomScript that exists.
│   │   ├── references.py          , Repointing Event Rules, permissions, Job history and schedules onto plugin rows. ACTION_SLUG is declared here and never imported from event_rules.py, so this tier does not become a second loader of a module PluginConfig is meant to resolve on its own.
│   │   ├── cleanup.py             , The last step, and the only one that deletes. Scoped by the map the cutover froze, never by build_map(), whose keys shift as modules are deleted. Gated on all four reference steps, because a captured schedule is recreated through the rows this pass removes. Refuses per module when deleting it would destroy Job history or an Event Rule, because both reach it through a GenericRelation the collector follows, and sorts those refusals into RETAINED and BLOCKED. Only blocked holds the run open: a module holding history of its own, or history for a class that left the file, is permanent, and treating it as outstanding would leave a migration that can never close and no replacement that can ever open.
│   │   └── verification.py        , The five read-only checks, each naming which side it read. Reads the LATEST run rather than the open one, since current() excludes migrated and a finished run is the one worth verifying. A reference the journal captured and the repoint left behind is a warning, one absent from the journal appeared after the cutover and is the only genuine fault.
│   ├── branching.py               , NetBox Branching integration: GLOBAL_MODELS main-schema routing for all five models, safety checks.
│   ├── execution.py               , run_script(): the transaction, request-processor and event context one run happens inside. The only home of the five undocumented NetBox symbols execution needs, so the requested generic core context replaces one file. Also load_script_class(): a stored row to a live class through the revision its project serves, unloaded before it returns, so the class is good for introspection rather than a run. Model-aware, which is why it is here and not in the runtime tier.
│   ├── validation.py              , validate_revision(): lease claim, fenced verdicts, error classifier, sanitizer, Module result persistence, published-script record.
│   ├── activation.py              , activate_revision() / deactivate_revision() domain orchestrators + synchronize_scripts(): the CustomScript upsert-and-retire pass, no imports.
│   ├── jobs.py                    , ProjectStorageCleanupJob (cleanup rechecks references under the project lock) + ProjectReconciliationJob (stages the Data Source directory as it stands at run time, and activates what a reverted directory resolves to) + ProjectEntrypointRefreshJob (restages the stored tree under the current selection, the only route an uploaded project has to apply one) + RevisionValidationJob (activates through activation.activate_revision on a valid verdict when the policy allows) + CustomScriptJob (pins the revision at enqueue for a one-shot run and pins nothing for a recurrence, which resolves the active revision per occurrence because JobRunner re-enqueues a periodic job with the same kwargs, then resolves the class out of it, runs it through execution.run_script, and sanitizes the run record before it reaches the Job row, and carries an optional event payload from enqueue onto the instance, which must stay JSON-safe because it rides on the Job row) + the seven migration jobs: MigrationInventoryJob (reports what a migration would do, writing nothing) + MigrationStagingJob (refuses on any blocking finding, then stages the proposed Projects) + MigrationCutoverJob (captures, then closes what a plugin can) + MigrationActivationJob (puts the staged Projects into service, so the CustomScript rows a reference can name exist) + MigrationReferencesJob (Event Rules, permissions, Job history and schedules, in that order, history before schedules so preservation happens before anything new is created) + MigrationCleanupJob (deletes the mapped modules, skipping any whose deletion would take Job history or an Event Rule with it) + MigrationVerificationJob (the five read-only checks). Every migration job imports the migration tier locally, because staging reaches ingestion, which imports this module. Each is started from the Migration page, which is the only trigger any of them has. All but the inventory and the verification check branching.unsafe_routing_reason() first, because enqueue-time safety does not carry to a job that may run much later on another pod, and those two need no check because they write nothing.
│   ├── signals.py                 , Revision deletion enqueues storage cleanup, and a completed Data Source sync enqueues one reconciliation per project on it. Wired in AppConfig.ready().
│   ├── event_rules.py             , RunCustomScriptAction, the registered `netbox_custom_scripts.run` action, plus the `event_rule_actions` list PluginConfig loads by convention, so no AppConfig entry is needed. A thin adapter onto CustomScriptJob.enqueue_run(), which already owns pinning and the executability check, and it reports a script it cannot run rather than raising, because one rule's misconfiguration must not end the batch. validate() refuses only retirement, since a disabled script is temporary state an administrator flips back. **This module needs no version guard and must not grow one**: PluginConfig resolves the list only where DEFAULT_RESOURCE_PATHS carries the key, which no 4.6 release does, so nothing below the 4.7 line ever imports it.
│   ├── choices.py                 , ProjectSourceTypeChoices, ActivationPolicyChoices, RevisionStatusChoices, ModuleDiscoveryStatusChoices, MigrationStateChoices.
│   ├── validators.py              , [CustomScriptProject] normalize_data_path(): canonical data_path form, shared by model clean() and the REST serializer.
│   ├── utils.py                   , source_path_to_dotted_name(): the one home of the path-to-module rule. data_source_relative_path(): the one home of the segment-wise data_path rule, shared by candidate listing, ingestion and migration staging.
│   ├── constants.py               , Storage limits, revision status groupings, validation lease bounds, published-script field bounds.
│   ├── management/commands/runcustomscript.py , The shell route to one run, for a self-hosted operator. Additive only: it duplicates the REST run route, which is what Cloud and Enterprise use, so it carries a cloud-compat waiver rather than breaching the contract. Named runcustomscript because NetBox ships its own runscript until v5.0 and the earlier app in INSTALLED_APPS wins the name, so a plugin command called runscript would never be reachable. Runs immediately in the calling process and exits non-zero unless the Job completed, which the built-in command never did. Refuses a --user that matches nobody rather than falling back to the first superuser.
│   ├── ingestion.py               , ingest_upload() / ingest_data_source() / current_source_tree() / uploaded_source_path() / declare_entrypoint() / check_upload_conflicts(): the one home of source ingestion. check_upload_conflicts() holds the replacement and case-collision rules, so the Add Script form and the REST upload action cannot drift. Upload declares the file it carries, a synchronized directory declares nothing. declare_entrypoint() is public because migration staging shares it, so the rule that a declaration is reused rather than replaced has one home.
│   ├── object_actions.py          , ActivateRevision + AddScript + ReconcileSource + RunScript ObjectAction subclasses, with button templates under templates/.../buttons/. Each takes the model's own action rather than change: activate, reconcile and run. RunScript renders inert rather than hidden when the script cannot run. AddScript and ReconcileSource each render only for the source type they belong to.
│   ├── template_content.py        , [add as needed] PluginTemplateExtension classes (cross-model UI).
│   └── templates/netbox_custom_scripts/
│       ├── customscriptproject.html              , [CustomScriptProject] Detail-view template, extends `generic/object.html`.
│       ├── customscriptprojectrevision.html       , Detail-view template for one revision, extends `generic/object.html`.
│       ├── migration.html         , The Migration landing page, which extends `generic/_base.html` rather than an object template. Lists the last inventory's blocking findings above the buttons, because staging refuses on any of them and creates nothing, and counts the warnings instead, because that list is one entry per module.
│       ├── migrationrun.html      , Detail-view template for one migration attempt.
│       ├── migration_stage.html   , Confirmation for staging.
│       ├── migration_cutover.html , Confirmation for the fence. Says there is no way back, in those words.
│       ├── migration_cleanup.html , Confirmation for the deletion, naming that the stored source goes with it.
│       └── *.html                 , [add as needed] Per-model detail templates and bulk-action forms.
├── docs/                          , mkdocs site (zensical primary, mkdocs compatible).
├── scripts/
│   └── check_cloud_compat.py      , AST checker for the Cloud / Enterprise platform contract (pre-commit hook).
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

Three models, all installation-global (`GLOBAL_MODELS` in `branching.py` routes
them to the main schema under NetBox Branching). `CustomScriptProject`:
one project = one script source tree = one Python package boundary, owning
either uploaded content or a Data Source directory, never both, with frozen
identity fields (`key`, `source_type` immutable, `storage_key` never changes).
`CustomScriptProjectRevision`: one immutable snapshot of the tree plus the
entrypoint configuration it was staged under, identity = project + source
digest + entrypoint digest, moved through its lifecycle by the storage and
validation services only. `CustomScriptModule`: one declared
entrypoint per row, author-editable declaration fields, system-managed
discovery fields, enabled declarations frozen into each revision's entrypoint
snapshot at staging time. `CustomScript`: one published Script
class per row, parented on the **project** rather than the Module, because
`script_order` lets a helper-defined class publish and helpers have no Module
row, so the publishing entrypoint is provenance in the revision snapshot instead.
Rows are derived from an activated revision, never authored: `enabled` is the
administrator's and synchronization never writes it, while retirement replaces
deletion so accumulated Job history survives.

### Source ingestion

`ingestion.py` is the one entry point that turns supplied files into a revision
on its way to a verdict: it declares the entrypoints the source implies, stages
the tree, and enqueues validation. Ordering is load bearing, because a revision
freezes the project's enabled declarations into its entrypoint snapshot at
staging time, so declarations are committed before staging. The content write
stays outside that transaction, since a rollback around stored bytes would leave
content no revision row names and therefore nothing to record its cleanup.

It has two callers, and they differ only in what the source implies. Upload
(a create form for a new project, an Add Script view for an existing one)
declares the file it carries, because every uploaded file is an entrypoint.
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
And `CustomScriptProject.clean()` refuses two projects on one Data Source whose data paths
are ancestor and descendant, so a folder inside another script-holding folder joins it.
That is not a compromise: `ingest_data_source()` stages everything under `data_path`, so
the higher Project already holds the deeper file, and a migrated Project therefore holds
the helper modules beside a script, which the built-in feature could never synchronize.

**Staging activates nothing**, and every Project it creates takes the manual policy for that reason
alone: `RevisionValidationJob` activates a valid revision under any other one. The passes past the
fence do activate, rewrite and delete, and they take a permission of their own for it. Every pass is
re-runnable, so a write fence over the built-in feature is not a dependency of this tier. Every
symbol the tier reads is listed in
[`docs/development/netbox-internals.md`](./docs/development/netbox-internals.md), together
with the export service that would replace them.

**Past the fence, three rules carry the whole design and each one comes from the models rather than
from preference.** The order is forced: activation precedes the reference migration, because an
`EventRule.action_object_id` has to name a `CustomScript` row and activation is what creates those,
and Job history is repointed before anything is deleted, because `JobsMixin.jobs` is a
`GenericRelation` and `Script.module` is `CASCADE`. **Every pass past the fence replays the map the cutover
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
advisory lock on the two-integer keyspace, so it cannot collide with NetBox's own
single-bigint keys, and it does not hold a database transaction open across
backend I/O. Two deliberate exclusions: deletion takes no lock, because the
cleanup job rechecks references under it before reclaiming anything, and
validation takes it only for its row transitions, because the lease already owns
the long import span.

Activation is the domain operation and promotion is the storage primitive, and the
primitive cannot be called without a synchronizer. `storage.service.promote_revision()`
takes a **required** `on_promote` callback and invokes it inside the transaction that
moves the pointer, after both row locks and the identity recheck, so an active revision
and the `CustomScript` rows derived from it change together. `activation.activate_revision()`
is the only caller that passes a real one. A default would leave that bypass one call
away, which is why the parameter is required and why the rename from `activate_revision`
was worth roughly thirty test call sites: a missed one fails loudly instead of silently
skipping synchronization. The callback also runs on the already-active path, so
re-activating the current revision repairs rows, and because synchronization skips a row
that already matches, the repair writes nothing when nothing is wrong.

### Integration points with NetBox

The standard hooks every NetBox plugin uses. Fill in the per-plugin details
inline as the plugin grows.

- **PluginConfig**, `netbox_custom_scripts/__init__.py` declares `name`,
  `label`, `verbose_name`, `description`, `version`, `author`,
  `author_email`, `base_url`, `min_version`, `max_version`. Add a
  `ready()` method that imports `signals` once you create that module.
- **Default model (CustomScriptProject)**, the scaffold ships a
  worked example object across every subsystem: model
  (`models/project.py`),
  table (`tables/project.py`),
  forms (`forms/<type>/project.py`, by type: model_forms, bulk_edit, bulk_import, filtersets),
  filterset (`filtersets/project.py`),
  seven views (`views/project.py`),
  URL routes (`urls.py`), nav menu (`navigation.py`),
  search index (`search.py`),
  REST API (`api/views.py`, `api/serializers/project.py`, `api/urls.py`),
  GraphQL (`graphql/{schema,types,filters,enums}.py`),
  and a per-area test suite covering model / API / view / table / form /
  filterset / GraphQL surfaces. Each test area mirrors that area's module layout:
  a flat area gets `tests/test_<area>.py`; a subpackage area gets
  `tests/<area>/test_project.py` for the worked
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
  `netbox_custom_scripts.<perm>`.
- **Signals**, `signals.py` is the home for cross-model side-effects.
- **Search**, `search.py` registers `SearchIndex` subclasses for major models
  so they appear in NetBox's global search.
- **Cross-model UI**, `template_content.py` registers
  `PluginTemplateExtension` subclasses that extend NetBox-core (or other
  plugin) detail pages.

## Commands

There is no Justfile / Makefile in this repo; commands are raw. Run them
inside a NetBox checkout that has this plugin installed with
`NETBOX_CONFIGURATION=configuration` exported and `$PWD/testing` on
`PYTHONPATH` (see the `## Development` section for setup).

| Command | What it does |
|---|---|
| `pip install -e '.[dev,test]'` (from this repo) | Install the plugin in editable mode with dev + test extras |
| `python netbox/manage.py test netbox_custom_scripts.tests -v 2` | Run the plugin's test suite |
| `ruff check .` | Lint |
| `ruff format .` | Format |
| `pre-commit install` | Install the pre-commit hook into `.git/hooks` |
| `pre-commit run --all-files` | Run every default-stage hook against the whole tree |
| `pre-commit run --hook-stage manual check-manifest` | Run `check-manifest` (manual stage), exercise before tagging a release |
| `python netbox/manage.py makemigrations netbox_custom_scripts` | Generate Django migrations after model changes |
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

   The shipped config sets `PLUGINS = ['netbox_custom_scripts']` and points
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
`manage.py makemigrations netbox_custom_scripts`, `related_name` changes are
no-op SQL but still need a migration for Django's state graph. Squash
periodically.

## Testing

- Tests use Django's `unittest.TestCase` (`django.test.TestCase`), **not**
  pytest. Suites live in `netbox_custom_scripts/tests/`.
- Run via NetBox's test runner:

  ```bash
  python netbox/manage.py test netbox_custom_scripts.tests -v 2
  ```

  The runner uses NetBox's settings and creates a real test database, so any
  code that touches the ORM, views, or APIs is exercised end-to-end.
- **Do not mock the database.** Use NetBox's test client and real fixtures;
  model-layer tests cover validators, constraints, and computed properties.
- **Query-count baselines.** NetBox 4.6+ view and REST API list tests assert
  each model's SQL query count against a baseline in
  `netbox_custom_scripts/tests/query_counts.json`. The scaffold ships a baseline
  for the worked example, so a fresh render passes. After you add or change a
  model with a list view, regenerate it by running the suite once with
  `UPDATE_QUERY_COUNTS=1` serially (the recorder rejects `--parallel`), then
  commit the updated file:

  ```bash
  UPDATE_QUERY_COUNTS=1 python netbox/manage.py test netbox_custom_scripts.tests
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
    regression.** Both legs are `continue-on-error` for exactly this reason. The
    baseline moves when the pinned ref moves, not before. Confirm the cause by
    running the same test against a pristine tree (`git archive HEAD` into a
    scratch directory, then point `PYTHONPATH` at it) before touching the file.

### Reporting test results

When reporting test results to a human reviewer, include:

- The exact command run.
- The working directory.
- Pass/fail count.
- The full failure output for any failing test.

Do not claim a test passed without running it.

## CI/CD

Three GitHub Actions workflows ship pre-wired under `.github/workflows/`:

- **`test.yml`**, PR / branch validation. Two jobs: a fast `lint` job
  running `pre-commit run --all-files`, followed by a `test` matrix
  (Python versions x `[v4.7.0-beta1, feature]`) that runs only if
  `lint` passes. The `feature` leg is the moving 4.7 canary and reports
  without blocking (`continue-on-error`). Postgres and Redis
  service containers. Triggers on pull requests and pushes to `main`.
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
3. `python netbox/manage.py makemigrations netbox_custom_scripts`.
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
   `templates/netbox_custom_scripts/`.
5. Register a `SearchIndex` in `search.py` if the model should be globally
   searchable.
6. Add test classes for the new model, mirroring the `CustomScriptProject` classes the
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
3. Add the template under `templates/netbox_custom_scripts/`. Detail layouts
   use `netbox.ui.layout.SimpleLayout` with panel lists from `ui/panels.py`.
4. Wire URL prefixes in `urls.py` via `get_model_urls(APP_LABEL, '<model>')`.
5. Add the menu entry to `navigation.py` (with the right `permissions=[...]`).
6. For object-level buttons, add an `ObjectAction` subclass to
   `object_actions.py` and a button template under
   `templates/netbox_custom_scripts/buttons/`.

### Bump the supported NetBox version

1. Update `min_version` / `max_version` in `netbox_custom_scripts/__init__.py`.
2. Update `COMPATIBILITY.md`.
3. Update `netbox_test_min_ref` / `netbox_test_max_ref` (via `copier update`, or directly in the rendered `.github/workflows/test.yml`) to match the new supported floor / ceiling.
4. Run the suite locally against the new version.
5. Drop any shims that exist only for the now-unsupported NetBox versions.
6. Note any compatibility shims or breaking changes in `docs/releases.md`.

### Cut a release

1. Bump `version` in both `pyproject.toml` and `netbox_custom_scripts/__init__.py`.
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
  `netbox_custom_scripts` entry of `STORAGES`.
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
`netbox_custom_scripts/tests/storage/test_backend_contract.py` drive the
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
  scopes every route, so a model's segment is its own plural noun: `modules/`,
  `projects/`, giving `/api/plugins/custom-scripts/modules/`, never
  `custom-script-modules/`. Segments are the only thing this affects, since
  reverse names come from the model (DRF derives the router basename from
  `queryset.model`, and `register_model_view` / `get_model_urls` name UI routes),
  so renaming a segment changes no `reverse()` call, table, or menu item.
- **Detail layouts** use `netbox.ui.layout.SimpleLayout` with panel lists
  from `ui/panels.py`.
- **Object-level buttons** are `ObjectAction` subclasses in
  `object_actions.py`, paired with templates in
  `templates/netbox_custom_scripts/buttons/`.
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
- **Permissions namespaced** under `netbox_custom_scripts.<perm>`. Used by
  `navigation.py` menu items and view base classes.
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
  on all three models. Reconcile to the declared floor rather than accepting a
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
- **GraphQL choice fields** follow NetBox core: typed enums (from
  `graphql/enums.py`) appear on filter inputs only; object types expose raw
  choice values as strings.
- **Docstrings carry the contract, not the reasoning.** One line by default. It
  earns more lines only for behaviour a caller has to branch on, never for
  rationale: that a call does not raise on bad input, that it deduplicates, what
  it returns, what it raises. If a sentence explains *why* the code is written
  the way it is, it does not belong in a docstring. Three rules follow, and each
  names where the displaced prose goes instead:
  - Rationale about one specific line is an **inline comment at that line**,
    not a paragraph in the docstring. `storage/paths.py` (the `MAX_PATH_BYTES`
    budget) and `runtime/cache.py` (the `mkdir` mode and bytecode traps) are
    the reference examples.
  - Cross-cutting rationale (lock discipline, fail-closed policy, the
    database-alias contract) belongs in `docs/` and the Architecture section
    above, stated once and referenced. Do not restate it per module or per
    function.
  - A docstring that only rewords its own identifier and base class is noise.
    `class FooListView(generic.ObjectListView)` needs no docstring at all.

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
