# Authoring Custom Scripts

Custom Scripts are Python classes that extend NetBox with on-demand automation.
This page covers the authoring API that ships with the plugin and how a project
publishes its scripts. Uploads, execution, and scheduling are planned
follow-ups, so scripts written today validate and are discovered but cannot be
run through the plugin yet.

## A minimal Custom Script

Import the authoring API from `netbox_custom_scripts` and subclass `Script`:

```python
from netbox_custom_scripts import Script, StringVar


class RenameDevice(Script):
    class Meta:
        name = 'Rename Device'
        description = 'Renames a device to match the naming convention'

    new_name = StringVar(max_length=64)

    def run(self, data, commit):
        self.log_info(f'Requested name: {data["new_name"]}')
        return data['new_name']
```

Every Custom Script defines a `run(self, data, commit)` method. `data` carries
the cleaned form values for the script's variables, keyed by attribute name.
`commit` tells the script whether database changes should persist (`False`
means a dry-run). The value returned from `run()` becomes the script's output.

Subclass `BaseScript` instead of `Script` for shared helper classes that should
not themselves appear as runnable scripts.

## Variables

Variables render as form fields when the script runs and deliver cleaned values
through `data`. Every variable type accepts the common keyword arguments
`label`, `description`, `default`, `required` (default `True`), and `widget`.

| Class | Purpose | Type-specific arguments |
|---|---|---|
| `StringVar` | Character string | `min_length`, `max_length`, `regex` |
| `TextVar` | Multi-line text | |
| `IntegerVar` | Integer | `min_value`, `max_value` |
| `DecimalVar` | Decimal number | `min_value`, `max_value`, `max_digits`, `decimal_places` |
| `BooleanVar` | Checkbox (never required) | |
| `ChoiceVar` | One of several static choices | `choices` |
| `MultiChoiceVar` | Several static choices | `choices` |
| `ObjectVar` | A single NetBox object | `model`, `query_params`, `context`, `null_option`, `selector`, `quick_add` |
| `MultiObjectVar` | One or more NetBox objects | Same as `ObjectVar` |
| `FileVar` | An uploaded file | |
| `IPAddressVar` | IPv4 or IPv6 address without a mask | |
| `IPAddressWithMaskVar` | IPv4 or IPv6 address with a mask | |
| `IPNetworkVar` | IPv4 or IPv6 prefix | `min_prefix_length`, `max_prefix_length` |
| `DateVar` | A date | |
| `DateTimeVar` | A date and a time | |

Subclass `ScriptVariable` to define a custom variable type.

## Meta attributes

The inner `Meta` class carries the script's presentation and execution
defaults:

| Attribute | Default | Purpose |
|---|---|---|
| `name` | Class name | Human-friendly script name shown in the UI. |
| `description` | Empty | Short description of what the script does. |
| `field_order` | None | Pins the listed variables to the front of the form. Unlisted variables keep their declaration order. |
| `fieldsets` | None | Groups variables into named form sections, replacing the default single group. |
| `commit_default` | True | Initial state of the "Commit changes" checkbox. |
| `scheduling_enabled` | True | Reserved for scheduled execution (not implemented yet). |
| `notifications_default` | Always | Reserved for job completion notifications (not implemented yet). |
| `job_timeout` | None | Reserved for the execution runner (not implemented yet). |

## Logging

Scripts log through five level-specific methods, each accepting an optional
message and an optional related object:

```python
def run(self, data, commit):
    self.log_debug('Verbose detail')
    self.log_info('Something noteworthy')
    self.log_success('Something worked', obj=device)
    self.log_warning('Something looks off')
    self.log_failure('Something broke')
```

Each entry records a timestamp, the severity, the message, and a link to the
related object when one is given. `log_failure()` also marks the whole run as
failed. Messages are forwarded to the NetBox system log under the
`netbox.plugins.netbox_custom_scripts.scripts` namespace.

## Aborting a script

Raise `AbortScript` to stop execution cleanly with a message:

```python
from netbox_custom_scripts import AbortScript


def run(self, data, commit):
    if invalid_precondition:
        raise AbortScript('Explain why the script stopped')
```

## Publishing scripts from a project

A project offers scripts through its declared entrypoints, the
[Custom Script Modules](models/customscriptmodule.md). An entrypoint is one
Python file of the project tree, and project validation imports it and
publishes the `Script` subclasses its own body defines, alphabetically:

```text
my-project/
├── tools/
│   ├── deploy.py      <- declared entrypoint, its Script classes publish
│   └── naming.py      <- helper, importable but never published
└── audit.py           <- declared entrypoint
```

Helper modules need no declaration. Entrypoints import them with normal
relative imports (`from . import naming`, `from .tools import naming`), and a
root `__init__.py` executes once per revision like any package initializer.

To pin presentation order, or to publish a Script class that lives in a helper
module, list the classes in a `script_order` at the top of the entrypoint:

```python
from .helpers import SharedAudit

script_order = [SharedAudit, RenameDevice]
```

Every entry must be a `Script` subclass defined in this project, listed once.
Classes imported from installed packages never publish, and `BaseScript`
building blocks stay unpublished unless they also subclass `Script`.

Two published classes cannot share one module path and class name, within an
entrypoint or across a revision's entrypoints, while one class re-exported by
several entrypoints publishes once.

Module-level code runs when validation imports the entrypoint, not only when a
script executes, so keep module bodies to imports and definitions and put work
in `run()`. See [Runtime and Loading](runtime.md) for the loading model and
what makes a revision invalid.

## Differences from NetBox's built-in scripts

The authoring API is a behavior-compatible rewrite of the script authoring
surface built into NetBox. The deliberate differences:

- Zero-valued bounds are enforced. `IntegerVar(min_value=0)`,
  `IntegerVar(max_value=0)`, and `DecimalVar(decimal_places=0)` work as
  written, where the built-in implementation silently drops zero-valued
  constraints.
- `DateVar` and `DateTimeVar` attach their date-picker widgets to the
  variable itself. The built-in implementation mutates Django's `DateField`
  and `DateTimeField` classes process-wide when these variables are
  instantiated.
- Legacy Report behavior is not supported. Scripts must define `run()`, and
  report-style `test_*` methods, `pre_run()`, `post_run()`, and `run_tests()`
  are not part of the API.
- Scheduling and notification form fields (`_schedule_at`, `_interval`,
  `_notifications`) are absent until scheduled execution ships. The
  `scheduling_enabled`, `notifications_default`, and `job_timeout` Meta
  attributes are read but not yet consumed.
- `TextVar` honors an author-supplied widget instead of always forcing a
  textarea, matching the contract that every variable accepts `widget`.
  The built-in implementation replaces the widget unconditionally.
- Custom variable subclasses may declare `field_attrs` at class level. Every
  instance gets its own copy, so one variable declaration cannot leak labels
  or defaults into another. The built-in implementation shares the class
  dictionary across instances.
- `ScriptVariable`, `AbortScript`, and `LogLevelChoices` are part of the
  public import surface. The built-in implementation keeps some of these in
  unrelated modules.
- Storage-coupled members (`filename`, `source`, `get_module_and_script`) are
  absent. Discovery identifies scripts through the project, the logical module
  path, and the class name instead of file bookkeeping on the class.
- Discovery publishes only what the entrypoint itself defines or explicitly
  lists in `script_order`. The built-in implementation publishes any Script
  subclass bound in the module, including ones imported from installed
  packages.
- System log records use the
  `netbox.plugins.netbox_custom_scripts.scripts.<project key>.<module>.<Class>`
  namespace, so two projects publishing the same class name log apart. The
  built-in implementation logs under `netbox.scripts`, so operators with
  handlers or filters keyed to that name need to update their logging
  configuration.

A compatibility layer for existing scripts that import from `extras.scripts`
is planned.
