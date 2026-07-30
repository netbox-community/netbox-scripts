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
(4.6.0 to 4.7.99 at scaffold time).

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
│   ├── urls.py                    , urlpatterns for 'modules/' + 'projects/' + detail-only 'scripts/<int:pk>/' via get_model_urls, sorted. Segments never repeat the base_url.
│   ├── navigation.py              , PluginMenu 'Custom Scripts' with a Projects group. Modules get no nav item: a declaration is a Project setting.
│   ├── api/
│   │   ├── __init__.py            , [stub]
│   │   ├── urls.py                , router.register for 'modules' + 'projects' + 'scripts'.
│   │   ├── views.py               , CustomScriptModuleViewSet (select_related project) + CustomScriptProjectViewSet with its GET/PUT `entrypoints` action + read-only CustomScriptViewSet.
│   │   └── serializers/
│   │       ├── __init__.py        , Re-exports CustomScriptModuleSerializer, CustomScriptProjectRevisionSerializer, CustomScriptProjectSerializer, CustomScriptSerializer.
│   │       ├── revision.py    , CustomScriptProjectRevisionSerializer: no route, exists only so event serialization can resolve one by model name. Omits url/display_url, a revision has no detail route to reverse.
│   │       ├── script.py      , CustomScriptSerializer: read-only, importable as api.serializers.CustomScriptSerializer for event serialization.
│   │       ├── project.py     , [CustomScriptProject] CustomScriptProjectSerializer.
│   │       └── module.py      , CustomScriptModuleSerializer: nested project, discovery fields read-only, revision as a bare ID.
│   ├── filtersets/
│   │   ├── __init__.py            , Re-exports CustomScriptModuleFilterSet, CustomScriptProjectFilterSet.
│   │   ├── project.py             , [CustomScriptProject] CustomScriptProjectFilterSet with custom search().
│   │   └── module.py              , CustomScriptModuleFilterSet: project by id + key, discovery filters, custom search().
│   ├── forms/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports each by-type subpackage.
│   │   ├── model_forms/project.py   , [CustomScriptProject] CustomScriptProjectEditForm + CustomScriptProjectEntrypointsForm (reconciles the selection onto enabled).
│   │   ├── model_forms/module.py    , CustomScriptModuleEditForm (project + source_path frozen, so disabled on edit).
│   │   ├── bulk_edit/project.py     , [CustomScriptProject] CustomScriptProjectBulkEditForm.
│   │   ├── bulk_import/project.py   , [CustomScriptProject] CustomScriptProjectBulkImportForm.
│   │   ├── filtersets/project.py    , [CustomScriptProject] CustomScriptProjectFilterForm.
│   │   └── filtersets/module.py     , CustomScriptModuleFilterForm.
│   ├── migrations/                , [CustomScriptProject] 0001_initial.py; regenerate on schema change and re-pin deps to the v4.6.0 heads (see Conventions).
│   ├── models/
│   │   ├── __init__.py            , Re-exports CustomScript, CustomScriptModule, CustomScriptProject, CustomScriptProjectRevision.
│   │   ├── project.py             , CustomScriptProject(PrimaryModel) with identity/ownership invariants and entrypoint_candidates / declarable_entrypoints / select_entrypoints + CustomScriptProjectRevision (immutable content fields, entrypoint snapshot in identity, status lifecycle, validation lease fields).
│   │   ├── module.py              , CustomScriptModule(PrimaryModel): declared entrypoints, canonical importable source_path frozen with project after creation, sibling rejection by letter case and by module name, system-managed discovery fields.
│   │   └── script.py              , CustomScript(JobsMixin, PrimaryModel): one published Script class, identity project + module_path + class_name, description overrides the abstract base as an unbounded TextField, enabled (admin) separate from is_retired (sync).
│   ├── tables/
│   │   ├── __init__.py            , Re-exports CustomScriptModuleTable, CustomScriptProjectTable.
│   │   ├── project.py                 , [CustomScriptProject] CustomScriptProjectTable(PrimaryModelTable) + CustomScriptProjectRevisionTable(BaseTable), the history table with no list view. Its ActionsColumn carries only extra_buttons and needs exempt_columns to render, since BaseTable hides unselected columns.
│   │   └── module.py              , CustomScriptModuleTable: source_path is the linked column, revision column unlinked.
│   ├── tests/                     , Each area mirrors its module layout (flat file or subpackage).
│   │   ├── __init__.py            , [stub] Test discovery anchor.
│   │   ├── plugin_testing.py      , [shared] Plugin-aware view/API test mixins (always rendered).
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
│   │   ├── api/test_script.py     , CustomScriptSerializer route reversal, event serialization, read-only refusals.
│   │   ├── api/test_revision.py   , Revision serializer resolution by model name, rendering without a route, REST delete of an activated project.
│   │   ├── views/test_script.py   , CustomScript detail view + changelog rendering.
│   │   ├── views/test_revision.py , Activate/Deactivate buttons: round trip, refusals, permissions, and which button each status renders.
│   │   ├── api/test_module.py     , CustomScriptModuleAPIViewTestCase: read-only discovery fields, path canonicalization + refusals.
│   │   ├── views/test_module.py   , CustomScriptModuleTestCase(PluginTestCases.NestedObjectViewTestCase).
│   │   ├── tables/test_module.py  , CustomScriptModuleTableTestCase(TableTestCases.StandardTableTestCase).
│   │   ├── forms/test_module.py   , Edit / Filter form test cases.
│   │   ├── forms/test_entrypoints.py , Selection reconciles onto enabled, nested paths, missing declared paths.
│   │   ├── api/test_entrypoints.py , The projects/<id>/entrypoints/ GET + PUT contract.
│   │   ├── models/test_entrypoint_candidates.py , Candidate enumeration from DataFile and from the newest manifest.
│   │   ├── filtersets/test_module.py , CustomScriptModuleFilterSetTestCase(TestCase, ChangeLoggedFilterSetTests), every field filterable.
│   │   ├── graphql/test_module.py , CustomScriptModuleGraphQLTestCase: discovery enum matches the ChoiceSet, revision absent from the type.
│   │   ├── storage/               , Storage tier suites: config, paths, manifest, entrypoints, store, service, signals, jobs, branching, backend contract.
│   │   ├── runtime/               , Runtime tier suites: test_cache.py, test_naming.py, test_loader.py, test_discovery.py, test_introspection.py.
│   │   ├── scripts/               , Authoring API suites: test_base.py, test_variables.py, test_exports.py.
│   │   ├── test_validation.py     , Validation service + RevisionValidationJob suites (claim/reclaim, fencing, classification, sanitization, Module persistence).
│   │   ├── test_activation.py     , SynchronizeScriptsTestCase (upsert/retire/no-op-write semantics) + ActivateRevisionTestCase + PromotionCallbackTestCase (required callback, savepoint depth, rollback).
│   │   └── test_ingestion.py      , Ingestion ordering and failure modes, plus UploadToActiveTestCase: the whole slice end to end against real validation.
│   ├── views/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports the seven CustomScriptProject view classes.
│   │   ├── project.py                  , [CustomScriptProject] List/Detail/Edit/Delete/BulkEdit/BulkDelete/BulkImport views, the Entrypoints tab, the Revisions history tab, Upload (create) + Add Script (detail) upload views, and the Activate confirmation view.
│   │   ├── module.py               , List/Detail/Edit/Delete/BulkDelete views. No bulk edit or bulk import: selection happens on the Project.
│   │   ├── script.py               , CustomScriptView: detail only, actions = () since no clone/edit/delete route exists.
│   │   └── revision.py             , Activate + Deactivate POST views for one revision. Gated on the PROJECT's change permission, with the revision queryset narrowed to permitted projects.
│   ├── ui/
│   │   ├── __init__.py            , [CustomScriptProject] Re-exports CustomScriptProjectPanel + CustomScriptProjectSourcePanel.
│   │   └── panels.py              , [CustomScriptProject] CustomScriptProjectPanel (left) + CustomScriptProjectSourcePanel and CustomScriptProjectStatePanel (right) for the detail view layout, plus CustomScriptPanel and CustomScriptStatePanel and the two Module panels.
│   ├── search.py                  , [CustomScriptProject] CustomScriptProjectIndex(SearchIndex) registered via @register_search.
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
│   │   └── exceptions.py          , Runtime error taxonomy (cache, module path, import, discovery).
│   ├── scripts/                   , Authoring API: base.py (BaseScript/Script), variables.py, forms.py, logging.py, exceptions.py.
│   ├── branching.py               , NetBox Branching integration: GLOBAL_MODELS main-schema routing for all four models, safety checks.
│   ├── validation.py              , validate_revision(): lease claim, fenced verdicts, error classifier, sanitizer, Module result persistence, published-script record.
│   ├── activation.py              , activate_revision() / deactivate_revision() domain orchestrators + synchronize_scripts(): the CustomScript upsert-and-retire pass, no imports.
│   ├── jobs.py                    , ProjectStorageCleanupJob (cleanup rechecks references under the project lock) + RevisionValidationJob (activates through activation.activate_revision on a valid verdict when the policy allows).
│   ├── signals.py                 , Revision deletion enqueues storage cleanup, wired in AppConfig.ready().
│   ├── choices.py                 , ProjectSourceTypeChoices, ActivationPolicyChoices, RevisionStatusChoices, ModuleDiscoveryStatusChoices.
│   ├── validators.py              , [CustomScriptProject] normalize_data_path(): canonical data_path form, shared by model clean() and the REST serializer.
│   ├── utils.py                   , source_path_to_dotted_name(): the one home of the path-to-module rule.
│   ├── constants.py               , Storage limits, revision status groupings, validation lease bounds, published-script field bounds.
│   ├── ingestion.py               , ingest_upload() / current_source_tree() / uploaded_source_path(): the one source-ingestion entry point, shared by upload and later by Data Source sync.
│   ├── object_actions.py          , ActivateRevision + AddScript ObjectAction subclasses, with button templates under templates/.../buttons/.
│   ├── template_content.py        , [add as needed] PluginTemplateExtension classes (cross-model UI).
│   └── templates/netbox_custom_scripts/
│       ├── customscriptproject.html              , [CustomScriptProject] Detail-view template, extends `generic/object.html`.
│       └── *.html                 , [add as needed] Per-model detail templates and bulk-action forms.
├── docs/                          , mkdocs site (zensical primary, mkdocs compatible).
├── scripts/
│   └── check_cloud_compat.py      , AST checker for the Cloud / Enterprise platform contract (pre-commit hook).
├── testing/
│   └── configuration.py           , NetBox config used by the test workflow (maintainer-added; see Development).
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
them to the main schema under NetBox Branching). `CustomScriptProject` (concept
5.1): one project = one script source tree = one Python package boundary, owning
either uploaded content or a Data Source directory, never both, with frozen
identity fields (`key`, `source_type` immutable, `storage_key` never changes).
`CustomScriptProjectRevision`: one immutable snapshot of the tree plus the
entrypoint configuration it was staged under, identity = project + source
digest + entrypoint digest, moved through its lifecycle by the storage and
validation services only. `CustomScriptModule` (concept 5.3): one declared
entrypoint per row, author-editable declaration fields, system-managed
discovery fields, enabled declarations frozen into each revision's entrypoint
snapshot at staging time. `CustomScript` (concept 5.4): one published Script
class per row, parented on the **project** rather than the Module, because
`script_order` lets a helper-defined class publish and helpers have no Module
row, so the publishing entrypoint is provenance in the revision snapshot instead.
Rows are derived from an activated revision, never authored: `enabled` is the
administrator's and synchronization never writes it, while retirement replaces
deletion so accumulated Job history survives. Execution lands in a later PR per
the concept.

### Source ingestion

`ingestion.py` is the one entry point that turns supplied files into a revision
on its way to a verdict: it declares the entrypoints the source implies, stages
the tree, and enqueues validation. Ordering is load bearing, because a revision
freezes the project's enabled declarations into its entrypoint snapshot at
staging time, so declarations are committed before staging. The content write
stays outside that transaction, since a rollback around stored bytes would leave
content no revision row names and therefore nothing to record its cleanup.

Upload is its first caller (a create form for a new project, an Add Script view
for an existing one). Data Source reconciliation (P11) is the second and
inherits the same ordering and the same lock. Activation is not a separate
mechanism: the validation job promotes a valid revision when the project's
`activation_policy` says so, and the Activate view covers the manual policy.

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

  NetBox releases that predate the framework ignore the file.

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
  (Python versions x `[v4.6.0, v4.6.5, main, feature]`) that runs only if
  `lint` passes. The `feature` leg is the NetBox 4.7 pre-release canary
  and reports without blocking (`continue-on-error`). Postgres and Redis
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
- **No management commands.** Neither platform can run one on demand. Use a data
  migration or a `JobRunner` job.
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

Check this before designing anything that persists bytes. The concept sanctions
it in section 6.3.

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
  `makemigrations` pins whatever the local (newer) NetBox checkout has, which
  breaks the migration graph on the declared minimum. After every
  `makemigrations` run, re-pin the deps to the v4.6.0 heads:
  `('core', '0024_job_notifications')`,
  `('extras', '0138_customfieldchoiceset_choice_colors')`,
  `('users', '0016_default_ordering_indexes')`.
  The same floor rule applies to inherited field definitions: NetBox's 4.7
  `feature` line alters `OwnerMixin.owner` (adds `related_name='+'`), so a
  migration generated against that line encodes state no released 4.6.x has.
  Generate against (or reconcile to) the released floor.
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
