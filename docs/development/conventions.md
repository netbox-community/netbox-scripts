# Development conventions

Use these conventions when changing the plugin. For environment setup and the
contribution process, see [Contributing](contributing.md). The
[NetBox internals reference](netbox-internals.md) describes the framework contracts
that need particular care during upgrades.

## Code organization

Keep implementation in `netbox_scripts/` and use the existing topic packages.
Forms are grouped by type, then topic. Add related code to those modules rather
than introducing a new layer for a small change. Discuss larger reorganizations
in the issue before starting.

| Area | Location |
|---|---|
| Models and their exports | `models/<topic>.py` and `models/__init__.py` |
| Serializers | `api/serializers/<topic>.py` |
| UI views and templates | `views/<topic>.py` and `templates/netbox_scripts/` |
| Detail panels | `ui/panels.py` |
| Object actions | `object_actions.py`, with templates in `templates/netbox_scripts/buttons/` |
| Cross-model signal handlers | `signals.py`, registered during startup |
| Search registrations | `search.py` |
| Cross-model template extensions | `template_content.py`, when needed |
| Tests | `tests/`, mirroring the implementation layout |

Add exports where the package exposes them and keep `__all__` alphabetized.

## NetBox integration

Use NetBox's existing models, mixins, forms, serializers, filtersets and views
where they fit. Keep plugin behavior in the plugin rather than monkey-patching
NetBox. Record dependencies on undocumented interfaces in the
[internals ledger](netbox-internals.md#the-list) and its compatibility checker.

Register model views with `@register_model_view` and generate their routes with
`get_model_urls()`. Workflow views, such as migration passes, can use explicit
routes. Register REST viewsets through `NetBoxRouter`.

Use `SimpleLayout` for detail pages, with panels from `ui/panels.py`. Declare
supported actions on list views, detail views and tables. Removing a route alone
can leave a permission-driven button pointing to it.

### URLs, filters and permissions

URL segments should not repeat the plugin name. Use `projects/` and
`script-files/` beneath the configured `base_url`, not `netbox-script-files/`.
Reverse names come from the model, not the segment, so renaming a segment
changes no `reverse()` call.

Declare each foreign-key filter explicitly as
`<field>_id = ModelMultipleChoiceFilter(field_name='<field>', ...)` rather than
relying on `Meta.fields` to generate it. Set the filter form's `model` to the
model it filters, not to a related parent. Follow the existing
`db_collation='natural_sort'` pattern for code and identifier fields.

Permission checks use the `netbox_scripts` namespace. In `Meta.permissions`,
declare the bare action, such as `run`, not `run_netboxscript`. NetBox composes
the model-specific action when working with Object Permissions. See
[Permissions](../permissions.md) for the actions and object constraints.

For object-scoped authorization, use `has_perm(..., obj=...)` or a restricted
queryset. An objectless permission check does not establish access to the
particular row being changed.

GraphQL filter inputs use typed enums for choice fields. Object types expose
the stored strings.

## Model changes

Preserve each model's documented identity and field-ownership rules. For
`ScriptProject`, `key`, `source_type` and `storage_key` are immutable after
creation. Normalize `data_path` with the shared validator.

`QuerySet.update()` and raw SQL bypass model guards. Code using either must
preserve canonical values and the relevant invariants itself. Consult the
[Project reference](../models/scriptproject.md#invariants) before changing those
write paths.

## Database migrations

Include the migration with its model change and tests. Model-state changes need
a migration even when they produce no SQL. Discuss destructive changes,
nullability changes and large-table indexes before implementing them.

### Initial schema

While the initial schema is being prepared for release, the project keeps one
`0001_initial.py`. Coordinate regeneration with a maintainer and agree how to
handle development databases that have already applied it. Do not assume those
databases can be discarded.

Do not regenerate migration history already distributed in a release. Schema
changes after publication need an upgrade path from the published migrations.

### NetBox dependency pins

The initial migration deliberately uses these NetBox v4.6.0 dependency heads,
even though the plugin's supported runtime floor is newer:

```python
dependencies = [
    ('core', '0024_job_notifications'),
    ('extras', '0138_customfieldchoiceset_choice_colors'),
    ('users', '0016_default_ordering_indexes'),
]
```

Review generated dependencies rather than accepting the heads selected by a
newer development checkout. Preserve these entries when regenerating the
initial migration. They describe schema dependencies, not the plugin's supported
NetBox version range.

Resolve inherited-field differences rather than accepting a persistent
`makemigrations --check` failure.

### Data migrations

Use historical models through `apps.get_model()` rather than importing current
model classes. Keep data migrations fast and safe to retry. Required
`ContentType` rows may not exist before `post_migrate` runs, so obtain or create
them explicitly with the historical model's `get_or_create()`.

## Testing

Use Django's test runner and real models for database behavior. Exercise views
and APIs through NetBox's test client rather than mocking persistence. Use
transactional tests for concurrency and transaction boundaries. A sequential
`TestCase` alone does not demonstrate that an interleaving is safe.

Keep tests in the same commit as the implementation they cover. Report the
NetBox ref, command, configuration and result, including skipped or unrun checks.
Distinguish actual integration tests from checks that replace a framework or
persistence boundary. [Contributing](contributing.md#running-tests) covers setup
and test-service isolation.

### Query-count baselines

`netbox_scripts/tests/query_counts.json` tracks the newest pinned NetBox release
in `.github/workflows/test.yml`. Investigate changed counts before updating it.
Compare the same test on an unchanged plugin tree to distinguish a plugin
regression from an upstream change.

For an intentional update, select that exact pinned release, run the suite
serially with `UPDATE_QUERY_COUNTS=1`, and review the JSON diff. Do not combine
this with `--parallel` or regenerate against the moving `main` or `feature`
branches.

## Code style and documentation

Prefer readable code and NetBox's established patterns. Ruff's configuration
lives in `pyproject.toml`, including 120-character lines, single quotes and LF
line endings. Do not add a separate `ruff.toml`.

Hook configuration belongs in `.pre-commit-config.yaml`. Keep tool settings in
`pyproject.toml` where the tool supports it.

Docstrings describe the caller's contract. Use one line when it is enough, and
add detail for return values, exceptions or required transaction and resource
scope. Explain a non-obvious line beside that line. Put cross-cutting rationale
in the relevant documentation page. A docstring that only repeats its identifier
needs a useful description instead.

Avoid section-banner comments. Preserve the syntax of `cloud-compat: ok` markers
because the compatibility checker reads them. An exemption needs a reason, not
just a way to silence a check.

Write documentation for its audience and keep warnings explicit. Put setup
instructions in the contribution guide, operational behavior in the relevant
user or administrator page, and integration details in the internals reference.
Link to shared explanations instead of maintaining competing copies.

## Compatibility

Breaking API changes require at least one minor release of deprecation warning
before removal. Discuss incompatible changes first rather than assuming the
alpha label waives that rule.

When changing NetBox support, update the plugin's version bounds,
`COMPATIBILITY.md` and CI refs together. Check whether the Python requirement also
needs to change, and run the relevant tests and internals checks against the
new target.

Preserve applicable third-party notices and follow the
[contribution licensing guidance](contributing.md#contribution-licensing). Review
licensing and repository-specific changes before regenerating files from a
scaffold.
