# Runtime and Loading

This reference explains how revisions load, how validation reports problems
and what activation changes. It is intended for administrators troubleshooting
Projects and authors working with imports or discovery. Configure the
[runtime cache](configuration.md#runtime-cache) on the Configuration page.

Script code can load during validation, run-form preparation and execution.
Depending on the operation, it runs in an RQ worker, a NetBox web process or
the process running `runcustomscript`. Imported code has that process's
permissions. Treat write access to source storage accordingly. See the
[storage trust boundary](configuration.md#storage-trust-boundary).

**Keep imports, constructors and form-building code free of side effects.**
All three can run during a web request, before the Script is executed.

## The private namespace

Each Project and revision has a private Python namespace:

```text
_netbox_scripts_runtime.p_<project>.r_<revision>.<dotted path>
```

The Project and revision components use immutable internal storage identities.
This keeps module names separate:

- Different Projects can contain modules with the same name.
- Revisions of one Project can coexist in a process, allowing validation while
  another revision remains active.
- Project modules do not shadow installed packages. A local `requests.py` is
  available through a relative import, while `import requests` resolves the
  installed package.
- The loader does not modify `sys.path` or the working directory. One import
  finder, registered at startup, handles imports within the private revision
  namespace.

User-facing names, including logger names and stored validation errors, use the
Project key and Project-relative module paths rather than these generated names.

## How a script file loads

Loading follows four steps:

1. **Check the path.** Validate the manifest, check that the Script File has an
   importable dotted name and confirm that it belongs to the manifest. These
   checks happen before I/O.
2. **Prepare a verified tree.** The [runtime cache](configuration.md#runtime-cache)
   supplies a local directory matching the manifest. Verification happens
   before import. The revision's import lock prevents another import from
   reading a damaged tree while the cache moves it aside.
3. **Register the containers.** Create the namespace root and Project container
   as packages without Project code. Their creation does not execute source.
4. **Import the module.** Load the revision package from the verified tree,
   execute its root `__init__.py` if present, then import the Script File.
   Project files can use normal relative imports.

If an import fails, the loader removes the revision modules it registered,
including helpers imported by `__init__.py`. Other revisions are unaffected.
The original exception is preserved so validation can classify the failure.

## How legacy imports resolve

Inside a revision, an import hook redirects supported legacy authoring imports
under `extras` to the plugin's compatibility layer. Other `extras` attributes
still reach NetBox. The hook uses a separate builtins mapping for each module
and applies only within the private namespace.

NetBox's own `extras.scripts` is not replaced, so built-in Scripts can run
alongside plugin Scripts. The hook does not temporarily rebind a shared module,
so concurrent revision imports need no coordination for that redirect.

Compatibility depends on whether the host still provides the legacy module,
not on a version comparison. When NetBox removes `extras.scripts`, the hook
raises an import error directing authors to migrate their imports rather than
leaving the revision waiting on an environment repair.

See [legacy scripts](authoring.md#legacy-scripts) for supported import forms
and the changes authors need to make.

## What discovery publishes

After importing a Script File, discovery selects its classes:

- `Script` subclasses defined in that file are published alphabetically by
  their bound name.
- `script_order` sets presentation order and can include Script classes from
  other modules in the same revision. Each entry must be a `Script` subclass
  defined in that revision and appear only once.
- Classes from installed packages are not published. Shared classes that
  extend `BaseScript` without also extending `Script` stay unpublished.
- A class is published once, even when bound to several names.

Each published class has a Project-relative module path, such as `tools.deploy`,
and a logger named
`netbox.plugins.netbox_scripts.scripts.<project key>.<logical module>.<Class>`.
The Project key separates logs from different Projects. Logger names do not
contain a revision digest, so logging configuration survives new revisions.

## Import-safe module-level code

Keep module bodies to imports and definitions. Validation executes that code
before anyone requests a run.

- Do not access the network, database or filesystem at import time.
- Do not start threads or processes at import time.
- Exceptions raised by Project code during import, including `SystemExit`,
  make the revision invalid. See the classification rules below.

Put operational work in `run()` so it happens during Script execution, not
while a form is prepared or source is validated.

## How validation reaches a verdict

Validation imports every Script File in the revision's snapshot and runs
discovery. It takes a `materialized` revision to `valid` or `invalid`, unless
an environment failure prevents a verdict.

**Content errors produce an `invalid` verdict.** These include syntax errors,
missing revision modules, exceptions raised by Project code during import,
unimportable declared paths, publication conflicts and declared files absent
from the manifest.

**Environment failures leave the revision retryable.** Storage, cache, host
I/O and recoverable import failures return it to `materialized` and fail the
validation Job without recording a verdict. Not every import failure is an
environment failure: a module name the interpreter cannot resolve is treated
as a content error. This includes an uninstalled distribution and
`import helpers` when the intended file is the Project's own `helpers.py`.
Use a relative import for Project-local modules.

**An `invalid` verdict does not clear when the host changes.** Identical source
and Script File selection reuse the revision and its existing verdict.
Installing a missing distribution and uploading the same content therefore
does not revalidate it. A fresh verdict requires a new revision identity from
changed source or a different final Script File selection.

An empty Script File selection is valid and can be activated, but publishes
no Scripts. This differs from selecting files that all import successfully
but publish nothing. That revision is `invalid`, with
`no_scripts_published` errors. Its Script File rows show `no_scripts` and a
reason, including the base class when a file extends NetBox's own Script class.
A file that publishes nothing does not invalidate a revision when another
selected file publishes a Script. It still reports its own discovery result.
Do not select a helper as the only Script File.

Validation also builds each class's run form and resolves its fieldsets. A
variable that cannot become a form field, a reserved variable name, an unknown
variable in a fieldset or an overlong display name makes the revision invalid.
The results are recorded on the revision as its
[published Scripts](models/scriptprojectrevision.md).

Stored validation errors and validation Job logs use Project-relative paths
instead of internal runtime names, storage identities and cache paths.
Ownership and the validation lease are described on the
[revision page](models/scriptprojectrevision.md#the-validation-lease),
including how a validation whose worker stopped is taken over.

## What activation does

Activation sets the Project's active revision and publishes its
[Scripts](models/netboxscript.md) in one database transaction. The revision
switch and Script publication commit together.

**Activation does not import source.** It uses the discovery results already
recorded by validation.

Before that transaction, activation verifies the stored tree. Reading and
hashing files can take time, especially with remote storage. Keeping that work
outside the transaction limits how long database rows are locked. The Project's
advisory lock covers both verification and publication, preventing restaging
or reclamation between them.

Use **Repair Scripts** on the Project page to synchronize its published rows
with the active revision. It restores missing or changed rows and reports the
number changed. Matching rows are left alone, with no write or change log.
The Project-level and revision-level **Activate** routes refuse a revision
that is already active, so use **Repair Scripts** for this operation.

Repairs made through a request are recorded in the change log and queue update
events attributed to the requesting user. Background activation and repair do
not provide that change log or object-change event delivery. This distinction
concerns publication, not events from a committed Script run. See
[Event Rules](event-rules.md#scripts-as-event-sources).

Deactivation retires the active revision, clears the Project's active pointer
and retires its Scripts. It reads no storage and imports no source. Activation
and deactivation lock the Project row before the revision row to serialize
concurrent changes.
