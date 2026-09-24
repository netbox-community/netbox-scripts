# Authoring Scripts

Write Python classes to automate tasks in NetBox. This page covers the plugin's
authoring API and how Projects discover and publish Scripts. See
[Uploading](uploading.md) to add source and [Execution](execution.md) to run it.

## A minimal Script

Import the authoring API from `netbox_scripts` and subclass `Script`:

```python
from netbox_scripts import Script, StringVar


class PreviewName(Script):
    class Meta:
        name = 'Preview a name'
        description = 'Shows a proposed device name without changing anything'

    proposed_name = StringVar(max_length=64)

    def run(self, data, commit):
        self.log_info(f'Proposed name: {data["proposed_name"]}')
        return data['proposed_name']
```

Define `run(self, data, commit)` to perform the work. For UI, REST and
`runcustomscript` runs, `data` contains cleaned form values keyed by variable
name. Event Rules pass their action payload without Script form validation.
See [Event Rules](event-rules.md#what-the-script-receives).

`commit` indicates whether database changes should persist. `False` means a dry
run. The value returned from `run()` becomes the output.

Use `BaseScript` for shared helper classes that should not appear as runnable
Scripts.

## Variables

Variables become form fields. For form-based runs, their cleaned values reach
`run()` through `data`. All types accept `label`, `description`, `default`,
`required` and `widget`. Variables are required by default unless noted below.

| Class | Purpose | Type-specific arguments |
|---|---|---|
| `StringVar` | Character string. | `min_length`, `max_length`, `regex` |
| `TextVar` | Multi-line text. | |
| `IntegerVar` | Integer. | `min_value`, `max_value` |
| `DecimalVar` | Decimal number. | `min_value`, `max_value`, `max_digits`, `decimal_places` |
| `BooleanVar` | Checkbox, never required. | |
| `ChoiceVar` | One of several static choices. | `choices` |
| `MultiChoiceVar` | Several static choices. | `choices` |
| `ObjectVar` | One NetBox object. | `model`, `query_params`, `context`, `null_option`, `selector`, `quick_add` |
| `MultiObjectVar` | Multiple NetBox objects. | Same as `ObjectVar`. |
| `FileVar` | Uploaded file. | |
| `IPAddressVar` | IPv4 or IPv6 address without a mask. | |
| `IPAddressWithMaskVar` | IPv4 or IPv6 address with a mask. | |
| `IPNetworkVar` | IPv4 or IPv6 prefix. | `min_prefix_length`, `max_prefix_length` |
| `DateVar` | Date. | |
| `DateTimeVar` | Date and time. | |

Subclass `ScriptVariable` to create a custom variable type.

**Queued runs support only uploads that Django keeps in memory.** The run form
and REST endpoint reject disk-backed uploads before creating a Job. Django chooses
storage using the whole request size and `FILE_UPLOAD_MAX_MEMORY_SIZE`, so several
files or large fields can put even a small file on disk.

**Recurring runs cannot carry uploads.** Later occurrences would receive a file
an earlier occurrence may have read or closed. The request is rejected before a
Job is created. Use a one-shot run instead. An optional `FileVar` left empty does
not prevent recurrence.

## Meta attributes

Use the inner `Meta` class for presentation and execution defaults.

| Attribute | Default | Purpose |
|---|---|---|
| `name` | Class name | Display name, up to 255 characters. |
| `description` | Empty | Description shown in the UI, with no length limit. |
| `field_order` | None | Places listed variables first. Others keep their declaration order. |
| `fieldsets` | None | Groups variables into named form sections instead of one default group. |
| `commit_default` | True | Default state of **Commit changes**. |
| `scheduling_enabled` | True | Whether the form offers scheduling. Set `False` when the Script should not run unattended. |
| `notifications_default` | `'always'` | Default notification policy: `'always'`, `'on_failure'` or `'never'`. Other values are rejected during validation. |
| `job_timeout` | None | Worker timeout as positive seconds or an RQ duration string such as `'5m'`. Uses the system setting when unset. |

Operators can override `commit_default`, `notifications_default` and `job_timeout`.
Treat them as recommended defaults rather than guarantees. `scheduling_enabled`
remains the author's decision and cannot be overridden. See
[Overriding a script's execution defaults](execution.md#overriding-a-scripts-execution-defaults).

## Logging

Use the five logging methods below. Each accepts an optional message and related
object.

```python
def run(self, data, commit):
    self.log_debug('Verbose detail')
    self.log_info('Something noteworthy')
    self.log_success('Something worked', obj=device)
    self.log_warning('Something looks off')
    self.log_failure('Something broke')
```

Entries include a timestamp, level, message and link to the related object when
provided. They are also sent to the NetBox system log under
`netbox.plugins.netbox_scripts.scripts`.

`log_failure()` records a failure-level message and sets `self.failed`. It does
not stop execution, fail the Job or roll back changes. See
[Aborting a script](#aborting-a-script) to stop a run with a failure.

**Do not log credentials, tokens or unreviewed response bodies.** Logs are stored
on Jobs and read through NetBox's Job-view permission, `core.view_job`.

The plugin removes its runtime identities, storage keys, digests and cache paths
from log messages and string output. It does not detect secrets or recursively
sanitize structured output. The Job retains its revision digest to identify the
source that ran.

## What a run knows about its own context

The following attributes are set before `run()` is called. Check for `None`
before using context that may not be available.

| Attribute | Set when | Holds |
|---|---|---|
| `self.request` | UI, REST or `runcustomscript` runs, or an Event Rule carrying a request. | The request used to attribute changes to its user. Event Rules pass a stripped copy without uploaded files. |
| `self.event` | An Event Rule started the run. | JSON-safe event context, including the type, object and rule. See [Event Rules](event-rules.md). |

A manually requested run has no event context:

```python
def run(self, data, commit):
    if self.event is None:
        self.log_info('Started by hand.')
    else:
        self.log_info(f'Started by the rule {self.event["event_rule"]}.')
```

## Aborting a script

Raise `AbortScript` to stop execution with an explanatory message:

```python
from netbox_scripts import AbortScript


def run(self, data, commit):
    if invalid_precondition:
        raise AbortScript('Explain why the script stopped')
```

## Publishing scripts from a project

Projects discover Scripts through declared [Script Files](models/scriptfile.md).
Validation imports those files and discovers the `Script` subclasses defined
in them, in alphabetical order.

```text
my-project/
├── tools/
│   ├── deploy.py      <- declared script file, its Script classes publish
│   └── naming.py      <- helper, importable but never published
└── audit.py           <- declared script file
```

Helpers need no declaration. Import them with relative imports, such as
`from . import naming` or `from .tools import naming`. A root `__init__.py`
serves as the Project's package initializer. See
[Runtime and Loading](runtime.md) for import behavior.

To set presentation order or publish a class from a helper module, list the
classes in `script_order` at the top of the Script File:

```python
from .helpers import SharedAudit

script_order = [SharedAudit, PreviewName]
```

Each entry must be a `Script` subclass defined in the Project and listed once.
Classes from installed packages do not publish. `BaseScript` helpers remain
unpublished unless they also subclass `Script`.

Distinct published classes cannot share a module path and class name, within one
Script File or across the revision. A class re-exported by several Script Files
publishes once.

Module-level code runs during validation imports, not just execution. Keep module
bodies to imports and definitions, and put work in `run()`. See
[Runtime and Loading](runtime.md) for validation and loading details.

Discovered classes become [Scripts](models/netboxscript.md) when the revision is
activated. Identity follows the defining module, even when `script_order`
re-exports the class elsewhere.

## What validation checks about a class

Validation builds the run form so configuration errors are found before a user
tries to run the Script. It rejects:

- Variable arguments Django cannot use to build a field, such as `max_length`
  on a field type that does not accept it.
- Variable names reserved by the run form, such as `_commit`.
- `Meta.fieldsets` entries that do not name declared variables.
- A `Meta.name` longer than 255 characters.

## Differences from NetBox's built-in scripts

The plugin reimplements the built-in authoring API, with these differences:

- Zero-valued bounds are honored. `IntegerVar(min_value=0)`,
  `IntegerVar(max_value=0)` and `DecimalVar(decimal_places=0)` do not lose their
  constraints as they do in the built-in implementation.
- `DateVar` and `DateTimeVar` attach picker widgets to their variables instead
  of changing Django's field classes process-wide.
- Legacy Report behavior is not supported. Scripts need `run()`. The Report
  methods `test_*`, `pre_run()`, `post_run()` and `run_tests()` are not part of
  the API.
- Recurrences resolve the active revision for each occurrence. This follows
  source updates, like the built-in scheduler using a module's current source,
  rather than pinning the whole schedule to one revision.
- Recurring uploads are rejected. The built-in implementation accepts them but
  reuses file objects that an earlier occurrence may have read or closed.
- `TextVar` honors an explicitly supplied widget rather than always using a
  textarea.
- A custom `ScriptVariable` can declare `field_attrs` at class level. Each
  instance receives its own copy, preventing labels or defaults from leaking
  between declarations.
- `ScriptVariable`, `AbortScript` and `LogLevelChoices` are public imports from
  the plugin's API, rather than being spread across unrelated modules.
- Storage-coupled members `filename`, `source`, `findsource()` and
  `get_module_and_script` are absent. Discovery uses Project, logical module
  path and class identity instead.
- `self.storage` is not available. Revision source is immutable and identified
  by its digest. Changing it would fail later verification. Persist data in a
  NetBox model or a separate backend provided by the deployment instead.
- The Report harness and `self._current_test` are absent. Report-style classes
  are rejected rather than emulated.
- Discovery publishes classes defined by the Script File or explicitly selected
  in `script_order`, not every imported Script subclass bound in that module.
- System logging uses
  `netbox.plugins.netbox_scripts.scripts.<project key>.<module>.<Class>` instead
  of `netbox.scripts`. Update handlers and filters that depend on the old logger
  name.

## Legacy scripts

Existing `extras.scripts` imports continue to resolve through the compatibility
layer. Source is stored unchanged. Check the
[differences](#differences-from-netboxs-built-in-scripts) above when migrating
Scripts that use APIs outside the supported surface.

These import forms are supported, including aliases and wildcards:

```python
from extras.scripts import Script, StringVar  # named, aliased, or a wildcard
import extras.scripts  # dotted, with or without "as"
from extras import scripts  # the submodule from the package
import extras  # then extras.scripts.Script
```

Imports inside functions and methods work the same way. Dynamic imports do not
use the compatibility layer:

```python
importlib.import_module('extras.scripts')  # reaches NetBox, not this plugin
```

A class based on that dynamically imported module uses NetBox's Script base,
not the plugin's. It does not publish and validation marks the revision `invalid`,
naming the class and base. If NetBox no longer provides that module, the import
fails instead and validation leaves no verdict for that attempt. Use an import
statement to reach the compatibility layer.

Other `extras` imports are unchanged. For example,
`from extras.models import Tag` still imports NetBox's model.

### Legacy Reports are refused, not emulated

A class with `test_*` methods and no `run()` makes the revision `invalid`, with an
error naming the class. Convert the Report to a Script by defining
`run(self, data, commit)` and moving its work there.

### The compatibility layer is transitional

Legacy imports work while NetBox still provides `extras.scripts`, which the
authoring and migration guides expect to end at NetBox v5.0. Once the module is
removed, legacy imports fail with a message pointing to the plugin's API.
No configuration is needed for the compatibility layer while it is available.

Use `netbox_scripts.scripts` for new Scripts. Update existing authoring imports
before that upgrade:

```python
from netbox_scripts.scripts import Script, StringVar
```

### What the supported surface covers

The stability guarantee covers the authoring API documented here. Scripts can
also import other modules available in the NetBox environment, but those APIs
are outside the plugin's guarantee. Undocumented NetBox imports may break on a
NetBox upgrade even when this plugin has not changed.
