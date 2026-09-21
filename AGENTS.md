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

`netbox-scripts` is a NetBox plugin: Custom Scripts for NetBox. It is owned by
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

**This map says what each file holds. The why lives in `docs/`** and is not repeated here:
[migration.md](./docs/migration.md), [permissions.md](./docs/permissions.md),
[execution.md](./docs/execution.md), [runtime.md](./docs/runtime.md),
[authoring.md](./docs/authoring.md), [configuration.md](./docs/configuration.md),
[data-sources.md](./docs/data-sources.md), [uploading.md](./docs/uploading.md),
[event-rules.md](./docs/event-rules.md), the four [models/](./docs/models/) pages and
[development/netbox-internals.md](./docs/development/netbox-internals.md).

```text
.
├── netbox_scripts/
│   ├── __init__.py                , PluginConfig, and AppConfig.ready() imports signals, branching and compat.
│   ├── urls.py                    , UI routes via get_model_urls, plus eight explicit 'migration/' paths.
│   ├── navigation.py              , PluginMenu 'Scripts': a Projects group and a Scripts group.
│   ├── api/
│   │   ├── urls.py                , NetBoxRouter registrations for the four endpoints.
│   │   ├── views.py               , Four viewsets, the run / upload / script-files actions, the lock-order mixin, two permission classes.
│   │   └── serializers/           , One module per topic: projects, revisions (read-only), scripts, upload, run.
│   ├── filtersets/                , One FilterSet per model, by topic module.
│   ├── forms/                     , By type then topic: model_forms, bulk_edit, bulk_import, filtersets, confirmations.
│   ├── migrations/                , 0001_initial.py. Regenerate on schema change, then re-pin the deps.
│   ├── models/
│   │   ├── projects.py            , ScriptProject + ScriptProjectRevision. Identity invariants, the source markers.
│   │   ├── scripts.py             , NetBoxScript + ScriptFile. Derived rows, execution overrides, discovery fields.
│   │   └── migration.py           , MigrationRun. State machine, one open run, the journal writers.
│   ├── tables/                    , One table per model, plus the four fed dictionaries rather than querysets.
│   ├── tests/                     , Mirrors the module layout. plugin_testing.py holds the shared composites.
│   ├── views/                     , projects, scripts, revisions, migration. Object views via
│   │                                @register_model_view, the Migration pages by explicit path.
│   ├── ui/panels.py               , Detail-view panels for every model.
│   ├── search.py                  , Three SearchIndex registrations.
│   ├── graphql/                   , schema, types, filters, enums.
│   ├── storage/                   , config, paths, manifest, script_files, store, locks, service, exceptions.
│   ├── runtime/                   , cache, loader, naming, discovery, introspection, resolution, exceptions.
│   ├── scripts/                   , Authoring API: base, variables, forms, logging, exceptions.
│   ├── compat/                    , The legacy `extras` import seam and its Report marker.
│   ├── migration/                 , source (the ONLY reader of the built-in feature), dialects, plan, mapping,
│   │                                staging, cutover, references, cleanup, verification, locking.
│   ├── branching.py               , GLOBAL_MODELS main-schema routing for all five models, safety checks.
│   ├── execution.py               , run_script() and script_class_context(): the context one run happens inside.
│   ├── permissions.py             , The two object-level rechecks: Script File rows, and the gated source fields.
│   ├── validation.py              , validate_revision(): lease, verdict, classifier, sanitizer.
│   ├── activation.py              , activate_revision / deactivate_revision + synchronize_scripts().
│   ├── ingestion.py               , The one entry point from supplied files to a revision awaiting a verdict.
│   ├── jobs.py                    , Five project jobs, NetBoxScriptJob, and the seven migration passes.
│   ├── signals.py                 , Revision deletion enqueues cleanup, a completed sync enqueues
│   │                                reconciliation, a declaration delete serializes on the write lock.
│   ├── event_rules.py             , RunNetBoxScriptAction and the `event_rule_actions` list PluginConfig loads.
│   ├── choices.py                 , Five ChoiceSets.
│   ├── validators.py              , normalize_data_path().
│   ├── utils.py                   , source_path_to_dotted_name(), data_source_relative_path().
│   ├── constants.py               , Storage limits, status groupings, lease bounds, field bounds, the gated source fields.
│   ├── management/commands/runcustomscript.py , The shell route to one run. Additive, carries a cloud-compat waiver.
│   ├── object_actions.py          , Five ObjectAction subclasses with templates under templates/.../buttons/.
│   ├── template_content.py        , [add as needed] PluginTemplateExtension classes.
│   └── templates/netbox_scripts/  , Per-model detail templates, the Migration page, eight confirmations.
├── docs/                          , mkdocs site (zensical primary). The home for all rationale.
├── scripts/
│   ├── check_cloud_compat.py      , AST checker for the Cloud / Enterprise contract (pre-commit hook).
│   └── check_netbox_internals.py  , Resolves every crossing netbox-internals.md lists. One blocking CI job.
├── testing/configuration.py       , The test config. Owns the Redis isolation (see Traps).
├── .github/workflows/             , test.yml, release.yml.
├── COMPATIBILITY.md               , Plugin to NetBox version matrix.
└── pyproject.toml                 , Metadata and dependency pins.
```

## Traps

Each of these has cost someone an hour. They are here because no docs page owns them.

- **An action filters by permission, never by route.** `ActionsMixin` and `ActionsColumn` render a
  button for any action the viewer holds the permission for, whether or not the URL exists. Declare
  `actions` explicitly on the list view, the detail view AND the table. `tests/views/test_actions.py`
  derives its inventories from the packages for that reason.
- **An inert button's title needs a wrapping span.** Tabler sets `pointer-events:none` on both
  `.btn:disabled` and `.btn.disabled`, so a `title` on the control never surfaces.
- **`_nullify` is the branch core takes instead of `changed_data`** (`bulk_views.py`). It arrives on
  the request, so a bulk-edit form never sees a "Set null" tick. Gate in the view.
- **`restrict_form_fields` narrows `data_source` to what the user may VIEW.** A disabled field
  holding an unviewable source fails validation before any gate is reached.
- **`PermissionsViolation.message` is a class attribute.** A constructor argument never reaches a
  reader of `e.message`. Set it on the instance.
- **`ValidatedModelSerializer.validate()` assigns the submitted values onto the instance** before
  `full_clean()`, so comparing against `self.instance` never fires. Re-read the stored row.
- **The two advisory locks share one keyspace.** Session-level `project_lock` and transaction-level
  `project_write_lock` conflict across sessions, so read protected state INSIDE the lock.
- **Redis is isolated in `testing/configuration.py`** at databases 15 and 14. Nothing else isolates
  it, and a committing test otherwise enqueues live jobs carrying test primary keys.
- **Never clear the RQ queue with `RQQueueTestMixin`**, which issues a server-wide `flushall()`.
- **`ManagedFile.storage` is a fresh instance while the loader reads the cached one**, so a migration
  fixture needs a real temporary directory, never in-memory storage.
- **`has_perm(perm)` without an object answers for SOME object.** Core grants it whenever the user holds the
  permission on any row, so a constrained grant passes. Pass `obj=` for one object, `restrict()` for a queryset.
- **Migration dependencies stay pinned at the v4.6.0 heads.** `makemigrations` names whatever the
  local checkout has. See Conventions.


## Architecture

Five models, all installation-global. **`ScriptProject`**: one source tree, either uploaded or a
Data Source directory, never both, with `key`, `source_type` and `storage_key` frozen after
creation. **`ScriptProjectRevision`**: one immutable snapshot of that tree plus the script file
configuration it was staged under, identified by project + source digest + script file digest.
**`ScriptFile`**: one declared script file, author-editable declaration fields and system-managed
discovery fields. **`NetBoxScript`**: one published Script class, derived from an activated revision
and never authored, parented on the Project rather than the Script File because `script_order` lets
a helper-defined class publish. **`MigrationRun`**: one attempt at moving off the built-in feature,
infrastructure rather than domain content, so no REST, GraphQL or list view.

Four rules the code will not let you break, each documented where it is enforced:

- **`ingestion.py` is the only entry point** from supplied files to a revision awaiting a verdict.
  Ordering is load bearing: declarations are committed before staging, because staging freezes the
  enabled ones into the revision's snapshot. See [uploading.md](./docs/uploading.md) and
  [data-sources.md](./docs/data-sources.md).
- **Activation is the domain operation, promotion is the storage primitive**, and the primitive
  cannot be called without a synchronizer: `promote_revision()` takes a required `on_promote`
  callback. See [models/scriptprojectrevision.md](./docs/models/scriptprojectrevision.md).
- **Every operation touching stored content holds `project_lock()`**, keyed on the immutable
  `storage_key`. Database-only changes hold `project_write_lock()` on the same key. See
  [development/netbox-internals.md](./docs/development/netbox-internals.md) and Traps.
- **`migration/source.py` is the only module that reads the built-in feature**, so the export
  service this needs from NetBox Community is a change to one file. The tier reads and never
  imports: classification parses stored source with `ast`, because an inventory must not execute an
  operator's code. See [migration.md](./docs/migration.md).

Compatibility with the built-in authoring API is one seam: revision modules execute with their own
builtins mapping whose `__import__` resolves the name `extras` through a plugin-owned stand-in.
Scoping is by module name, not by time, so nothing process-wide is rebound. See
[runtime.md](./docs/runtime.md).

### Integration points with NetBox

The standard plugin hooks: `PluginConfig` in `__init__.py`, a `PluginMenu` in `navigation.py`,
`@register_model_view` plus `get_model_urls()` for UI routes, a `NetBoxRouter` in `api/urls.py`,
`SearchIndex` registrations in `search.py`, cross-model side effects in `signals.py` wired from
`AppConfig.ready()`, and `PluginTemplateExtension` in `template_content.py`. Permissions are
namespaced `netbox_scripts.<perm>`.

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

  **The baseline is a single file, but the CI matrix spans four NetBox refs, and a
  core change to query behaviour lands on them at different times.** The file tracks
  the newest pinned release in `.github/workflows/test.yml`, and two rules follow:

  - **Regenerate against a checkout at that ref, never against `main` or
    `feature`.** `UPDATE_QUERY_COUNTS=1` rewrites every key it observes, so a run
    on a moving ref silently records counts the pinned legs will reject. The local
    development checkout floats across branches, so check which line it is on
    first.
  - **A count that changes only on `main` or `feature` is core's, not a
    regression.** `feature` is `continue-on-error` for exactly this reason.
    `main` is not, so a query change landing there turns a blocking leg red for a
    change that is not ours: read it, then decide whether the baseline moves or the
    plugin does. The baseline moves when a pinned release moves, not before. Confirm
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

Two GitHub Actions workflows ship pre-wired under `.github/workflows/`:

- **`test.yml`**, PR / branch validation. Four jobs, three of them gated on
  a fast `lint` job running `pre-commit run --all-files`: a `test` matrix
  (Python versions x `[v4.7.0, v4.7.1, main, feature]`, twelve legs, coverage
  collected once on py3.14 / `main`), `test-branching` (one leg
  with NetBox Branching installed, the two branching test modules only), and
  `internals`, which resolves every symbol `docs/development/netbox-internals.md`
  lists against the `feature` ref. Only the `feature` test leg and the branching
  job report without blocking (`continue-on-error`), so `main` breaking is ours to
  answer. `internals` is the one blocking check against `feature`: with no database,
  services or fixtures, a failure there is a crossing, the plugin skipped outside its
  version range, or a settings or setup change. Postgres and Redis service containers for
  the two test jobs. Triggers on pull requests and pushes to `main`.
- **`release.yml`**, Build + `twine check` + publish to PyPI through Trusted
  Publishing (`pypa/gh-action-pypi-publish`, `id-token: write`, environment
  `pypi`). Triggers on published GitHub releases. The Trusted Publisher
  registered on the PyPI project is the only credential.

## Common Tasks

This repo uses topic subpackages for every area, and by-type-then-topic for forms. The layout is
settled, so there is one way to do each of these.

### Add a new model

1. Add the class to its topic module `models/<topic>.py` and re-export from `models/__init__.py`,
   `__all__` alphabetised. Extend `PrimaryModel` for tags, custom fields and comments, or
   `BaseModel` for a line item. Add `ContactsMixin` if contacts apply.
2. Add a `ChoiceSet` to `choices.py` for any new enum.
3. `makemigrations netbox_scripts`, then re-pin the deps to the v4.6.0 heads.
4. Add the filterset, table and serializer to their topic modules and re-export each. Forms need no
   re-export edit, `forms/__init__.py` star-imports each by-type module. Then update `api/urls.py`,
   `urls.py`, `navigation.py` and the per-model template.
5. Register a `SearchIndex` in `search.py` if it should be globally searchable.
6. Add a test class per surface in `tests/<area>/test_<topic>.py`, mirroring `ScriptProject`'s.
   **Tests land in the same commit as the implementation.**

### Add a REST API endpoint

1. Add the serializer to `api/serializers/<topic>.py` and re-export it. `NetBoxModelSerializer` for
   a `PrimaryModel`, `ValidatedModelSerializer` for a `BaseModel`.
2. Add the viewset to `api/views.py`, extending `NetBoxModelViewSet`.
3. Register the route in `api/urls.py` via the `NetBoxRouter`.
4. Make sure a `FilterSet` exists, with an explicit
   `<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)` for every FK.
5. Add the endpoint's test class to `tests/api/test_<topic>.py`.

### Add a UI view

1. Add the view to `views/<topic>.py`, re-export it, and decorate with `@register_model_view`. Use
   the `netbox.views.generic` bases. Do not add explicit URL patterns for object views.
2. Add the table to `tables/<topic>.py` and re-export it.
3. Add the template under `templates/netbox_scripts/`, using `SimpleLayout` with panels from
   `ui/panels.py`.
4. Wire the prefix in `urls.py` via `get_model_urls(APP_LABEL, '<model>')`.
5. Add the menu entry to `navigation.py`, declaring **every** permission the route enforces.
6. For an object button, add an `ObjectAction` to `object_actions.py` and a template under
   `templates/netbox_scripts/buttons/`. See Traps: declare `actions` on all three surfaces.

### Bump the supported NetBox version

1. `min_version` / `max_version` in `netbox_scripts/__init__.py`.
2. `COMPATIBILITY.md`, then the pinned refs in `.github/workflows/test.yml`.
3. Run the suite against the new version and drop any shims for the dropped releases.
4. Note breaking changes in `docs/releases.md`.

### Cut a release

Bump `version` in `pyproject.toml` and `netbox_scripts/__init__.py`, update `docs/releases.md`, then
tag and publish a GitHub release. `release.yml` builds and publishes to PyPI.


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
- **`Meta.permissions` declares the BARE action**, `('run', ...)` not `('run_netboxscript', ...)`,
  matching `core.DataSource`'s `('sync', ...)`. The backend composes
  `f'{app_label}.{action}_{model_name}'` from whatever was ticked, so a codename carrying the model
  name grants something nothing checks. Consequence: the bare codename also produces a bare Django
  permission row that `RemoteUserBackend` reads, so a plain Django grant of one of these five does
  not reach the view. Object Permissions are the route, per
  [permissions.md](./docs/permissions.md).
- **Migrations.** One squashed `0001_initial.py` until the schema settles. Data migrations use
  `RunPython` with `apps.get_model()` and `get_or_create`, since `ContentType` rows may not exist yet.
- **Migration dependencies stay at the v4.6.0 heads**, even though the floor is 4.7.0:
  `('core', '0024_job_notifications')`, `('extras', '0138_customfieldchoiceset_choice_colors')`,
  `('users', '0016_default_ordering_indexes')`. `makemigrations` pins whatever the local checkout
  has, so re-pin after every run. Graphs are append-only, so these still resolve. Do not "correct"
  them. The same floor rule covers inherited fields: reconcile rather than tolerate a standing
  `makemigrations --check` diff.
- **Project identity invariants.** `key` and `source_type` immutable after creation, `storage_key`
  never changes, `data_path` canonical via `validators.normalize_data_path()`. `QuerySet.update()`
  bypasses all of it and must supply canonical values itself.
- **GraphQL choice fields:** typed enums on filter inputs only, object types expose raw strings.
- **Docstrings carry the contract, not the reasoning.** One line by default. More lines only for
  behaviour a caller branches on: what it returns, what it raises, that it does not raise on bad
  input. If a sentence explains *why*, it goes elsewhere: rationale about one line is an inline
  comment at that line, cross-cutting rationale goes in `docs/`. A docstring that only rewords its
  own identifier states nothing, so improve it rather than deleting it. No section-banner comments,
  split the module instead. The `cloud-compat: ok` markers are exempt, the checker needs their form.
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
- License: [`LICENSE`](./LICENSE).
- User docs (mkdocs): [`docs/`](./docs/).
- NetBox plugin docs: <https://netboxlabs.com/docs/netbox/plugins/>.
- Repository: <https://github.com/netbox-community/netbox-scripts>.
