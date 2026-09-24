# AGENTS.md, netbox-scripts

## Methodology precedence

This file contains instructions for AI agents only. Read
[CONTRIBUTING.md](./CONTRIBUTING.md) for the contribution process and
[Development conventions](./docs/development/conventions.md) for shared engineering
rules. Keep contributor-facing guidance in those documents, not here.

Use the project's instructions rather than a tool's generic workflow when they
conflict. Raise conflicting project instructions with the maintainer rather than
silently choosing a policy.

- Keep each feature's implementation and tests in one atomic commit. Parallel
  work must preserve those commit boundaries.
- Tests land with the implementation they cover. Do not write tests in
  red/green/refactor cycles before implementation under the current workflow.
- Existing `*_PLAN.md` or `PROJECT.md` files record in-flight work. Update the
  relevant plan rather than creating a second plan for the same task.

## Repository Overview

NetBox Scripts is an Apache-2.0 plugin for NetBox. Its Django app is
`netbox_scripts`, and its repository is
[netbox-community/netbox-scripts](https://github.com/netbox-community/netbox-scripts).
Some initial files came from the NetBox Labs plugin scaffold. Repository-specific
code, configuration and accepted decisions now take precedence over template
examples.

Read versions and tool settings from their source files:

- `pyproject.toml`: package version, Python requirement, dependencies and tools.
- `netbox_scripts/__init__.py`: plugin version and NetBox `min_version` /
  `max_version`.
- `COMPATIBILITY.md`: the documented release matrix.
- `.github/workflows/test.yml`: the test matrix and required checks.

Keep these aligned when changing support. Do not infer compatibility from a
moving local checkout or an old example in documentation.

## Tech Stack

- Python, Django and Django REST Framework, using the selected NetBox runtime.
- Django's test runner with `TestCase` and `TransactionTestCase`, not pytest.
- Ruff through pre-commit, configured in `pyproject.toml` and
  `.pre-commit-config.yaml`.
- Zensical for documentation, with `mkdocs.yml` and the pinned `docs` extra.
- NetBox's `manage.py` for application and test commands.

## Repository Map

This map locates responsibilities. Behavioral explanations belong in `docs/`.
Read the relevant guide before changing its implementation.

```text
netbox_scripts/
  __init__.py              PluginConfig and startup registration
  api/                    Routes, viewsets, action schemas and serializers
  filtersets/             Model filters, grouped by topic
  forms/                  Forms grouped by type, then topic
  migrations/             Django schema history
  models/
    projects.py           ScriptProject and ScriptProjectRevision
    scripts.py            NetBoxScript and ScriptFile
    migration.py          MigrationRun state and journal
  tables/                 Model and dictionary-backed tables
  tests/                  Tests mirroring the package layout
    plugin_testing.py     Shared test composites
  views/                  Project, Script, Revision and migration views
  ui/panels.py            Detail-view panels
  templates/              Model pages, action buttons and confirmations
  urls.py                 Registered model routes and migration routes
  navigation.py           Plugin menu
  search.py               Search registrations
  graphql/                Schema, types, filters and enums
  storage/                Backends, manifests, paths, snapshots, locks and promotion
  runtime/                Verified cache, imports, discovery and class resolution
  scripts/                Public authoring API, variables, forms and logging
  compat/                 Legacy authoring imports and Report detection
  migration/              Inventory, staging, cutover, references and cleanup
    source.py             Built-in feature reads
  branching.py            Global-model routing and safety checks
  execution.py            Per-run transaction, request and event context
  permissions.py          Object-scoped declaration and source-field checks
  validation.py           Validation lease, verdicts and error classification
  activation.py           Activation, deactivation and Script synchronization
  ingestion.py            Source acceptance and processing handoff
  jobs.py                 NetBox JobRunner entry points
  signals.py              Cross-model synchronization and cleanup handoffs
  event_rules.py          Script action registration and dispatch
  choices.py              Choice sets
  validators.py           Data path normalization
  utils.py                Shared source-path conversions
  constants.py            Limits, status groups and protected-field definitions
  management/commands/
    runcustomscript.py    Additional synchronous execution route
  object_actions.py       Object actions paired with button templates

docs/                     User, administrator and developer documentation
scripts/
  check_cloud_compat.py    Static deployment-compatibility checks
  check_netbox_internals.py  NetBox integration canary
testing/                  Test and branching configurations
.github/workflows/        Tests, docs, security analysis, publishing and maintenance
COMPATIBILITY.md           Release compatibility matrix
CONTRIBUTING.md            Human contribution and development guide
pyproject.toml            Package metadata, dependencies and tool configuration
```

## Traps

Check these integration details when changing the affected paths:

- **Actions are permission-driven, not route-driven.** A button renders for any
  action the viewer holds, whether or not its route exists. Declare supported
  actions on list views, detail views and tables. `tests/views/test_actions.py`
  discovers every exported view and table itself, and fails on any action
  without a route.
- **Disabled buttons need a wrapping span for tooltips.** A `title` on a control
  with `pointer-events: none` is not enough.
- **Bulk edit uses `_nullify` separately from `changed_data`.** The view must
  account for requested null values when checking protected fields.
- **`restrict_form_fields` can reject a stored Data Source.** It narrows
  `data_source` to sources the user may view, so a disabled field holding an
  unviewable source fails validation before any permission gate runs.
- **`PermissionsViolation.message` is an attribute.** Set it on the exception
  instance when a custom message is required. Passing a constructor argument
  does not supply the message read by the view.
- **Serializer validation mutates its instance.** Re-read stored values when
  comparing a proposed change with the original. Do not treat `self.instance`
  as an unchanged copy after `ValidatedModelSerializer.validate()`.
- **Project advisory locks share a keyspace.** Session-level `project_lock`
  and transaction-level `project_write_lock` conflict across connections.
  Read protected state after taking the lock.
- **Redis needs separate test queues.** `testing/configuration.py` uses databases
  15 and 14, and nothing else isolates it. A committing test otherwise enqueues
  jobs that a live worker would run with test primary keys. Logical database
  numbers do not make a shared server safe from `FLUSHALL`.
- **Do not use `RQQueueTestMixin` to clear queues.** It calls server-wide
  `flushall()`.
- **Migration fixtures need real temporary source storage.** `ManagedFile.storage`
  and the built-in loader do not necessarily use the same backend instance.
- **`has_perm(perm)` without an object is not a target-object check.** It
  returns true when the user holds the permission on any object, so a
  constrained grant passes. Use `obj=` for one object or `restrict()` for a
  queryset.
- **Migration dependencies are deliberately pinned.** Preserve the
  [documented heads](./docs/development/conventions.md#netbox-dependency-pins), even
  when the development checkout generates newer dependencies.

## Architecture

The five models are installation-global:

| Model | Responsibility |
|---|---|
| `ScriptProject` | Source configuration and accepted/active revision state. `key`, `source_type` and `storage_key` are immutable after creation. |
| `ScriptProjectRevision` | Source and Script File snapshots, identified by Project plus source and selection digests. |
| `ScriptFile` | Editable declaration settings and system-managed discovery results. |
| `NetBoxScript` | A class published from a revision, with separate operator settings. Its parent is the Project because `script_order` can publish a helper-defined class. |
| `MigrationRun` | Migration state and journal. It has no general-purpose REST, GraphQL or list interface. |

Preserve these four contracts:

1. **Source enters through `ingestion.py`.** Declaration changes commit before
   staging freezes the enabled selection. Preserve the preconditions and
   permission checks that precede those writes. See [Uploading](./docs/uploading.md)
   and [Data Sources](./docs/data-sources.md).
2. **Activation and promotion remain separate.** `activation.py` owns the domain
   operation. `storage/service.py` owns the promotion primitive, whose required
   `on_promote` callback synchronizes Script rows inside the promotion transaction.
   Do not make it optional or merge the layers as a wording or cleanup change.
   See [Revisions](./docs/models/scriptprojectrevision.md).
3. **Coordinate the stored-source lifecycle.** Staging, refresh, promotion and
   reclamation hold `project_lock()` on the immutable
   `storage_key`. Database-only Project and declaration writes use
   `project_write_lock()` on that key. Validation uses its lease and conditional
   verdict writes, not a Project lock around user code. Preserve each operation's
   actual lock order. See [NetBox internals](./docs/development/netbox-internals.md).
4. **Built-in feature reads are isolated in `migration/source.py`.** Inventory
   classifies stored Python with `ast` and must not execute it. Migration writers
   operate on the records supplied by that layer. See [Migration](./docs/migration.md).

The legacy import adapter gives revision modules their own builtins mapping.
Its `__import__` resolves `extras` through a plugin-owned adapter, without replacing
NetBox's package process-wide. See [Runtime](./docs/runtime.md).

### Integration points with NetBox

Use `PluginConfig`, `PluginMenu`, `@register_model_view`, `get_model_urls()`,
`NetBoxRouter` and `SearchIndex` for their intended integration points.
Cross-model signal handlers are registered during startup. Use
`PluginTemplateExtension` when adding cross-model UI content.

Permissions are namespaced under `netbox_scripts`. Refer to the
[permission guide](./docs/permissions.md) before changing an action or write path.

## Commands

Run commands from the plugin repository root with its development environment
active. The examples assume a sibling NetBox checkout at `../netbox`. Use the
[contribution guide](./CONTRIBUTING.md#development-environment) to install the
requirements and isolate PostgreSQL, Redis and source storage first.

For tests and schema checks, select the shipped test configuration in that shell:

```bash
export PYTHONPATH="$PWD/testing${PYTHONPATH:+:$PYTHONPATH}"
export NETBOX_CONFIGURATION=configuration
```

| Command | Purpose |
|---|---|
| `python -m pip install -e '.[dev,test]'` | Install editable development and test dependencies |
| `python ../netbox/netbox/manage.py test netbox_scripts.tests -v 2` | Run the plugin suite |
| `pre-commit install` | Install commit hooks |
| `pre-commit run --all-files` | Run default-stage hooks over tracked files |
| `pre-commit run --hook-stage manual check-manifest` | Check source-distribution contents |
| `python scripts/check_netbox_internals.py --netbox ../netbox/netbox` | Check the NetBox integration ledger without a database |
| `python ../netbox/netbox/manage.py makemigrations netbox_scripts` | Generate a schema change for review |
| `python ../netbox/netbox/manage.py makemigrations --check --dry-run` | Check for unrecorded model changes |
| `python ../netbox/netbox/manage.py migrate` | Apply migrations to the selected development database |
| `python -m pip install -e '.[docs]'` | Install the pinned documentation tools |
| `zensical serve` | Preview documentation |
| `zensical build --clean --strict` | Run the documentation build gate |
| `python -m build` | Build a wheel and source distribution |

Ruff runs through pre-commit with the repository's pinned hook configuration.
Standalone `ruff check .` and `ruff format .` require a matching local installation.
Hooks using `--all-files` do not include untracked additions.

## Development

Use [CONTRIBUTING.md](./CONTRIBUTING.md#development-environment) as the development
recipe. Do not create a second setup based on local paths or scaffold defaults.

The shipped test configuration contains development-only credentials,
`DEVELOPER = True` and in-memory source storage. It is not a production or shared
web/worker configuration. Switch to a separate development configuration before
starting `runserver` or an RQ worker, and keep those workers away from test queues.

Follow the [database migration conventions](./docs/development/conventions.md#database-migrations)
before generating, regenerating or consolidating migrations.

## Testing

Follow the [testing conventions](./docs/development/conventions.md#testing),
including the [query-count baseline rules](./docs/development/conventions.md#query-count-baselines).
Use [CONTRIBUTING.md](./CONTRIBUTING.md#running-tests) for setup and service isolation.

### Reporting test results

Give the exact command, working directory, NetBox ref and test configuration.
Report passed, failed and skipped tests, plus the failure output needed to
understand any error. Keep secrets out of shared output.

Do not claim a test passed without running it. Distinguish source inspection,
controlled diagnostics and integration tests, and list checks not run.

## CI/CD

Read `.github/workflows/` for current triggers, version matrices and job gates.
Do not infer them from scaffold defaults or a fixed count of workflows here.

- `test.yml` defines lint, test, branching and internals checks. Preserve the
  distinction between blocking checks and advisory jobs.
- `docs.yml` builds the site with Zensical and controls Pages deployment.
- `codeql.yml` runs security analysis.
- `release.yml` validates packages and publishes through PyPI Trusted Publishing.
  Do not introduce a stored PyPI token as a replacement for that setup.
- Issue and thread maintenance workflows must preserve their intended issue or
  pull-request scope, including contributions from forks.

## Common Tasks

Use the existing topic packages. Group forms by type, then topic. Do not create a
new layer or move settled module boundaries merely to implement a small change.

### Add a new model

1. Add the class to `models/<topic>.py` and its alphabetized export to
   `models/__init__.py`. Use `PrimaryModel` or `BaseModel` as appropriate, and
   `ContactsMixin` only when contacts apply.
2. Add any new `ChoiceSet` to `choices.py`.
3. Generate and review the migration using the
   [migration conventions](./docs/development/conventions.md#database-migrations).
4. Add its filterset, table, serializer, forms and template in the existing
   topic modules. Update exports and routes as required.
5. Register search and navigation only for the surfaces the model should expose.
6. Add tests for those surfaces alongside the implementation.

### Add a REST API endpoint

1. Add and export its serializer from `api/serializers/<topic>.py`. Use
   `NetBoxModelSerializer` for a `PrimaryModel` or `ValidatedModelSerializer`
   for a `BaseModel`.
2. Add the viewset in `api/views.py` and register it with `NetBoxRouter` in
   `api/urls.py`.
3. Add explicit FK filters such as
   `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)`.
4. Cover the endpoint in `tests/api/test_<topic>.py`, including its permissions
   and allowed methods.

### Add a UI view

1. Add and export the view in `views/<topic>.py`. Use NetBox's generic bases and
   `@register_model_view` for model views.
2. Add its table and template. Detail layouts use `SimpleLayout` and panels from
   `ui/panels.py`.
3. Use `get_model_urls(APP_LABEL, '<model>')` for model routes. Migration's
   workflow views use explicit routes.
4. Add navigation with every required route permission.
5. Pair object actions with templates under `templates/netbox_scripts/buttons/`.
   Declare the supported actions on the view and table surfaces.

### Bump the supported NetBox version

Update `min_version` / `max_version`, `COMPATIBILITY.md` and the CI refs together.
Review the Python requirement when needed. Run the relevant suite and internals
checks against the new target. Remove a compatibility adapter only after its
supported use has ended, and document breaking changes in the release notes.

### Cut a release

Follow [Releasing](./docs/development/releasing.md). Maintainers own version
updates, release notes, tags and publication. Changing documentation does not
by itself authorize a release.

## Cloud and Enterprise compatibility (hard contract)

Keep the plugin usable on deployments with multiple web and worker processes,
including NetBox Cloud and NetBox Enterprise. These are design constraints, not
a statement that an alpha release is approved for either platform.

- **Write authoritative source through Django storage.** Resolve
  `STORAGES['netbox_scripts']` in `storage/config.py`. A `FileSystemStorage`
  backend is valid when every process can reach the same content. Do not bypass
  the backend with direct writes or rely on a private pod directory for source.
- **Keep runtime files disposable.** The manifest-verified runtime cache is the
  local executable-file layer. Rebuild it from storage and verify it before use.
  No authoritative application state may live only in a process or pod.
- **Do not make management commands the only route to a capability.** Managed
  deployments need an accessible UI/API operation, a migration or a JobRunner
  workflow as appropriate. `runcustomscript` is additive and carries an explicit
  `cloud-compat: ok` exemption. Its `--user` attribution differs from REST's
  token-user attribution.
- **Use NetBox jobs, not private schedulers or background threads.** Deletion
  records cleanup intent in its transaction and leaves storage reclamation to a
  job. Preserve the default-database and branching checks on both handoff and
  execution. This does not prohibit the synchronous storage operations used by
  upload and manual activation.
- **No per-pod application caches.** Redis is the shared cache on both
  platforms. The runtime import cache is the one exception, because imports need
  a local directory tree.
- **Keep the live request off objects.** Queued events and job payloads carry
  model instances to consumers that may pickle them. Read
  `netbox.context.current_request` instead. NetBox's token views can store
  it safely only because tokens queue no events.

Backend contract tests in `tests/storage/test_backend_contract.py` exercise
storage without filesystem paths or directory semantics. The static checker
`scripts/check_cloud_compat.py` checks filesystem calls, per-process state,
threads, shell commands, management commands and stored requests. The test
configuration's `EVENTS_PIPELINE` pickles every queued object. None of these
checks replaces another.

Keep justified `cloud-compat: ok` exemptions local to the exempt statement. Do not
add one merely to silence a failure. Runtime cache operations, the additive
management command and a Script run's request copy have explicit exemptions.

## Conventions and Patterns

Follow [Development conventions](./docs/development/conventions.md). That page
owns code organization, NetBox integration patterns, migrations, testing, style
and compatibility rules for contributors and agents alike.

Update the relevant public documentation when an approved convention changes.
Do not introduce a contributor requirement solely in this file or direct human
readers here for development guidance.

## Troubleshooting

For missing ContentTypes during a migration, follow the
[data-migration guidance](./docs/development/conventions.md#data-migrations).

## References

- [README](./README.md), [contribution guide](./CONTRIBUTING.md) and
  [compatibility matrix](./COMPATIBILITY.md).
- [Security policy](./SECURITY.md) and [license](./LICENSE).
- [Documentation](./docs/), built with Zensical.
- [NetBox plugin development](https://docs.netbox.dev/en/stable/plugins/development/).
- [Repository](https://github.com/netbox-community/netbox-scripts).
