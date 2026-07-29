# Runtime and Loading

This page describes how a stored revision becomes importable Python and how
project validation judges it. It is background for operators and script
authors, none of it requires configuration beyond the
[runtime cache](configuration.md#runtime-cache).

## The private namespace

Revision code never imports under a name an author chose. Every loaded module
sits below one private root, with one container per project and one package per
revision:

```text
_netbox_custom_scripts_runtime.p_<project>.r_<revision>.<dotted path>
```

The project and revision components come from internal storage identities that
never change. The consequences:

- Two projects can ship modules with identical names and never collide.
- Two revisions of one project coexist in one process, which is what lets a new
  revision be validated while the active one keeps serving.
- A revision module can never shadow an installed distribution. A project file
  named `requests.py` is importable as a relative module, while `import
  requests` inside project code still resolves the real package.
- Loading never touches `sys.path`, the working directory, or Python's global
  finder chain, so the host process is unaffected by what projects contain.

These generated names are internal. Everything user-facing, from logger names
to stored validation errors, uses the project key and project-relative module
paths instead.

## How an entrypoint loads

Loading follows four steps, each gated on the one before it:

1. **Check the path.** The manifest is validated, then the entrypoint must map
   to an importable dotted name and must be part of that manifest. All three
   checks run before any I/O.
2. **Materialize a verified tree.** The [runtime
   cache](configuration.md#runtime-cache) produces a local directory that
   provably holds exactly the manifest's files. Nothing imports from a tree
   that has not just been verified. Materialization holds the revision's import
   lock, because setting a damaged tree aside renames the directory a
   concurrent import is reading from.
3. **Register the containers.** The namespace root and the project container
   are synthetic packages holding no project code, so this step cannot fail on
   revision content.
4. **Import inside the failure boundary.** The revision package is built on the
   verified tree, its root `__init__.py` executes if the tree ships one, and
   the entrypoint imports as a normal submodule. Relative imports between
   project files work exactly as they would in an installed package.

A failed import sweeps every module it managed to register, including helpers a
root `__init__.py` pulled in before failing, so a broken revision leaves
nothing behind and a sibling revision is unaffected. The original exception is
preserved for classification.

Imports run inside the NetBox worker process that requested them. The worker is
the isolation boundary: importing a revision executes its module-level code
with the worker's permissions, which is why content is verified first and why
the [storage trust boundary](configuration.md#storage-trust-boundary) treats
write access to the store as equivalent to code execution.

## What discovery publishes

After an entrypoint imports, discovery decides which classes it offers:

- Every `Script` subclass the entrypoint module's own body defines is
  published, ordered alphabetically by bound name.
- A `script_order` list in the entrypoint pins presentation order, and it is
  also the one way to publish a Script class defined in another module of the
  revision. Entries must be Script subclasses defined in this revision, listed
  at most once.
- Classes imported from installed packages are never published, and shared
  building blocks that subclass `BaseScript` without `Script` are not
  published either.
- One class publishes once however many names it is bound to.

Each published class is stamped with its identity: the logical module path
(project-relative, such as `tools.deploy`) and a logger name of the form
`netbox.plugins.netbox_custom_scripts.scripts.<project key>.<logical
module>.<Class>`. The logger carries the stable project key and never a
revision digest, so two projects publishing the same class name log apart and
log routing survives new revisions.

## Import-safe module-level code

Validation imports entrypoints to judge them, so module-level code runs at
validation time, not only when a script is executed. Keep module bodies to
imports and definitions:

- Do not talk to the network, the database, or the filesystem at import time.
- Do not spawn threads or processes at import time.
- Raising at import time makes the revision invalid, including `SystemExit`.

Work belongs in `run()`, which executes only when a user runs the script.

## How validation reaches a verdict

Project validation drives a revision from `materialized` to `valid` or
`invalid` by importing every entrypoint in the revision's snapshot and running
discovery on it. The verdict rules:

- A verdict is a statement about revision content. Bad syntax, a reference to a
  revision module that does not exist, project code raising at import time, an
  unimportable declared path, a publication conflict, or an entrypoint missing
  from the manifest all make the revision `invalid`, terminally.
- Environment trouble never produces a verdict. A missing external
  distribution, an unreachable backend, cache failure, or host I/O failure
  rolls the revision back to `materialized` and fails the job, so a retry gets
  a fair attempt. An import naming a module the revision itself ships is
  content, not environment, so writing `import helpers` where the tree holds
  `helpers.py` reports as an authoring mistake rather than retrying forever.
- An empty entrypoint set is valid. A project whose revision declares no
  modules validates and can be activated, it simply offers no scripts.
- Stored validation errors are sanitized. Runtime namespaces, storage
  identities, and cache paths never appear in them or in job logs, module
  references read project-relative.

Ownership, the validation lease, and why a crashed validation recovers by
itself are described on the [revision page](models/customscriptprojectrevision.md).
