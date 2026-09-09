# Authoring Scripts

Scripts are Python classes that extend NetBox with on-demand automation.
This page covers the authoring API that ships with the plugin and how a project
publishes its scripts. See [Uploading](uploading.md) for getting a script into a
project and [Execution](execution.md) for running one.

## A minimal Script

Import the authoring API from `netbox_scripts` and subclass `Script`:

```python
from netbox_scripts import Script, StringVar


class RenameDevice(Script):
    class Meta:
        name = 'Rename Device'
        description = 'Renames a device to match the naming convention'

    new_name = StringVar(max_length=64)

    def run(self, data, commit):
        self.log_info(f'Requested name: {data["new_name"]}')
        return data['new_name']
```

Every Script defines a `run(self, data, commit)` method. `data` carries
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
| `name` | Class name | Human-friendly script name shown in the UI. At most 255 characters. |
| `description` | Empty | Short description of what the script does. Any length. |
| `field_order` | None | Pins the listed variables to the front of the form. Unlisted variables keep their declaration order. |
| `fieldsets` | None | Groups variables into named form sections, replacing the default single group. |
| `commit_default` | True | Initial state of the "Commit changes" checkbox. |
| `scheduling_enabled` | True | Whether the run form offers the scheduling fields. Set False for a script that is not safe to run unattended. |
| `notifications_default` | `'always'` | Initial value of the "Notifications" field on the run form. One of `'always'`, `'on_failure'` or `'never'`. Anything else is refused at validation. |
| `job_timeout` | None | How long a run may take before the worker stops it, as a positive number of seconds or an RQ duration string such as `'5m'`. Default is the system setting. |

An operator can override `commit_default`, `notifications_default` and
`job_timeout` per installation, so treat them as your recommended values rather
than as guarantees. `scheduling_enabled` takes no override, because it is your
statement that the script is safe to run unattended.
See [Overriding a script's execution defaults](execution.md#overriding-a-scripts-execution-defaults).

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
`netbox.plugins.netbox_scripts.scripts` namespace.

**What you log is persisted and readable.** The run log is stored on the Job row,
so anyone holding NetBox's `core.view_job` permission can read it, and it
outlives the run. Before it lands there the plugin scrubs its own runtime
identities out of the whole record, storage keys, digests and cache paths, and
your messages go through that same pass. Nothing looks for a secret. Treat
`log_*` like any other output destination: do not pass a credential, a token,
or a response body you have not looked at.

## What a run knows about its own context

Two attributes are set on the instance before `run()` is called. Each is `None`
when the run did not come with the thing it carries, so guard before reading it.

| Attribute | Set when | Holds |
|---|---|---|
| `self.request` | The run was requested through the UI, the REST API, or `runcustomscript`, or an Event Rule forwarded the request behind the change that triggered it | The requesting user's HTTP request. This is what attributes any changes to that user. An Event Rule forwards a stripped copy, without uploaded files. |
| `self.event` | An Event Rule started the run | The JSON-safe part of the event context, including `event_type`, `object_type`, `object_id` and the rule's own name. See [Event Rules](event-rules.md). |

A run somebody started by hand carries no event:

```python
def run(self, data, commit):
    if self.event is None:
        self.log_info('Started by hand.')
    else:
        self.log_info(f'Started by the rule {self.event["event_rule"]}.')
```

## Aborting a script

Raise `AbortScript` to stop execution cleanly with a message:

```python
from netbox_scripts import AbortScript


def run(self, data, commit):
    if invalid_precondition:
        raise AbortScript('Explain why the script stopped')
```

## Publishing scripts from a project

A project offers scripts through its declared script files, the
[Script Files](models/scriptfile.md). A script file is one
Python file of the project tree, and project validation imports it and
publishes the `Script` subclasses its own body defines, alphabetically:

```text
my-project/
├── tools/
│   ├── deploy.py      <- declared script file, its Script classes publish
│   └── naming.py      <- helper, importable but never published
└── audit.py           <- declared script file
```

Helper modules need no declaration. Script files import them with normal
relative imports (`from . import naming`, `from .tools import naming`), and a
root `__init__.py` executes once per revision like any package initializer.

To pin presentation order, or to publish a Script class that lives in a helper
module, list the classes in a `script_order` at the top of the script file:

```python
from .helpers import SharedAudit

script_order = [SharedAudit, RenameDevice]
```

Every entry must be a `Script` subclass defined in this project, listed once.
Classes imported from installed packages never publish, and `BaseScript`
building blocks stay unpublished unless they also subclass `Script`.

Two published classes cannot share one module path and class name, within a
script file or across a revision's script files, while one class re-exported by
several script files publishes once.

Module-level code runs when validation imports the script file, not only when a
script executes, so keep module bodies to imports and definitions and put work
in `run()`. See [Runtime and Loading](runtime.md) for the loading model and
what makes a revision invalid.

Each published class becomes a [Script](models/netboxscript.md) once the
revision is activated, identified by the module that defines it, so a class
re-exported through `script_order` keeps the identity of its own file.

## What validation checks about a class

Importing a class is not enough to prove it usable, so validation builds its run
form as well. A variable only stores its keyword arguments when the class is
defined, so a combination Django rejects stays invisible until something asks for
the form. Asking during validation means these become an invalid revision instead
of a failure at the first attempt to run:

- A variable Django cannot build a field from, for example a `max_length` its
  field type does not accept.
- A variable using a name the run form reserves, such as `_commit`. It would
  silently replace the form's own field.
- A `Meta.fieldsets` entry naming something that is not a variable. Fieldsets are
  not filtered, so an unknown name reaches the template and breaks the page.
- A `Meta.name` longer than 255 characters, which is more than the published
  Script can record.

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
- A recurring run resolves the project's active revision at each occurrence
  rather than carrying the revision it was created against. The built-in
  implementation has no revision concept, so there is nothing to compare
  against, but a scheduled run there executes whatever the module holds when it
  fires, which is the same intent.
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
- Storage-coupled members (`filename`, `source`, `findsource()`,
  `get_module_and_script`) are absent. Discovery identifies scripts through the
  project, the logical module path, and the class name instead of file
  bookkeeping on the class.
- **`self.storage` is absent by design, and that is a guarantee rather than an
  omission.** In the built-in feature it is the same `scripts` backend the
  module was loaded from, so a script could rewrite the source NetBox had just
  imported. Here a revision is immutable and content-addressed: its digest is
  its identity, and a script writing into its own tree would invalidate the
  digest every later verification checks against. A script that needs to
  persist something writes to a NetBox model, or to a backend the deployment
  gives it for that purpose.
- The Report harness is absent, `self._current_test` with it. Report-style
  classes are refused at discovery rather than emulated.
- Discovery publishes only what the script file itself defines or explicitly
  lists in `script_order`. The built-in implementation publishes any Script
  subclass bound in the module, including ones imported from installed
  packages.
- System log records use the
  `netbox.plugins.netbox_scripts.scripts.<project key>.<module>.<Class>`
  namespace, so two projects publishing the same class name log apart. The
  built-in implementation logs under `netbox.scripts`, so operators with
  handlers or filters keyed to that name need to update their logging
  configuration.

## Legacy scripts

A script written for NetBox's built-in runner imports its authoring API from
`extras.scripts`. That import keeps working here, so an existing script
publishes and runs as a Script with no edit at all. Your source is
stored exactly as you supplied it and is never rewritten.

Every form of the import resolves, whichever one the script happens to use:

```python
from extras.scripts import Script, StringVar  # named, aliased, or a wildcard
import extras.scripts  # dotted, with or without "as"
from extras import scripts  # the submodule from the package
import extras  # then extras.scripts.Script
```

An import inside a function or a method body resolves the same way. Only one
form is out of reach, because it does not go through the import statement at
all:

```python
importlib.import_module('extras.scripts')  # reaches NetBox, not this plugin
```

A script using that form publishes nothing, since the class it derives from is
NetBox's rather than this plugin's, so the revision is refused as `invalid` with
a message naming the class and the base it inherited. Once a NetBox release stops
shipping the module, the same line fails at import instead and the revision rolls
back for a retry with no verdict. Import it with a statement either way.

Imports of anything else under `extras` are untouched and reach NetBox, so
`from extras.models import Tag` is the real model.

### Legacy Reports are refused, not emulated

The Report dialect has no equivalent here, so a class that declares `test_*`
methods and no `run()` makes the revision `invalid` with a message naming the
class. That is deliberate: the alternative is publishing something that looks
runnable and fails only once an operator presses Run. Convert a Report by
giving the class a `run(self, data, commit)` method and doing the work there.

### The compatibility layer is transitional

Legacy imports work for as long as NetBox itself ships `extras.scripts`. When a
future NetBox release removes it, the import stops working and fails with a
message pointing at this plugin's own module. Nothing about your Project
changes in the meantime, and there is no configuration to set.

Treat it as a migration aid rather than a permanent interface. New scripts
should import from `netbox_scripts.scripts`, and migrating an existing
one is a single line per file:

```python
from netbox_scripts.scripts import Script, StringVar
```

### What the supported surface covers

The stability guarantee covers the authoring API described on this page. A
script may import anything else the NetBox environment provides, and real
scripts commonly do, but those imports are NetBox's surface rather than this
plugin's. A script reaching into an undocumented NetBox symbol can break on a
NetBox upgrade without anything in this plugin having changed.
