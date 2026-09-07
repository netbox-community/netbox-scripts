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
_netbox_scripts_runtime.p_<project>.r_<revision>.<dotted path>
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

## How a script file loads

Loading follows four steps, each gated on the one before it:

1. **Check the path.** The manifest is validated, then the script file must map
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
   the script file imports as a normal submodule. Relative imports between
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

## How legacy imports resolve

A revision module executes with its own copy of the builtins mapping, whose
import hook resolves the name `extras` through a stand-in this plugin owns. The
stand-in serves the two legacy authoring modules itself and passes every other
attribute through to the real package, which is what lets a script written for
the built-in runner publish unmodified. See [legacy
scripts](authoring.md#legacy-scripts) for the author-facing view.

The mapping is installed by the loader that executes each module, and those
loaders are reached through a finder that can only match names inside the
private namespace. Two consequences matter operationally. Nothing outside a
revision is affected, so NetBox's own `extras.scripts` is never replaced and its
built-in scripts keep running alongside this plugin's. And nothing is rebound
for a window of time, so two workers importing two revisions at once need no
coordination.

Whether the redirect serves or refuses is decided by asking the host whether it
still provides a module under the legacy name, never by comparing versions. Once
NetBox removes `extras.scripts`, the redirect raises an import error naming the
migration rather than standing aside, because an absent module would classify as
an environment fault and leave the revision with no verdict recorded at all.

## What discovery publishes

After a script file imports, discovery decides which classes it offers:

- Every `Script` subclass the script file's own body defines is
  published, ordered alphabetically by bound name.
- A `script_order` list in the script file pins presentation order, and it is
  also the one way to publish a Script class defined in another module of the
  revision. Entries must be Script subclasses defined in this revision, listed
  at most once.
- Classes imported from installed packages are never published, and shared
  building blocks that subclass `BaseScript` without `Script` are not
  published either.
- One class publishes once however many names it is bound to.

Each published class is stamped with its identity: the logical module path
(project-relative, such as `tools.deploy`) and a logger name of the form
`netbox.plugins.netbox_scripts.scripts.<project key>.<logical
module>.<Class>`. The logger carries the stable project key and never a
revision digest, so two projects publishing the same class name log apart and
log routing survives new revisions.

## Import-safe module-level code

Validation imports script files to judge them, so module-level code runs at
validation time, not only when a script is executed. Keep module bodies to
imports and definitions:

- Do not talk to the network, the database, or the filesystem at import time.
- Do not spawn threads or processes at import time.
- Raising at import time makes the revision invalid, including `SystemExit`.

Work belongs in `run()`, which executes only when a user runs the script.

## How validation reaches a verdict

Project validation drives a revision from `materialized` to `valid` or
`invalid` by importing every script file in the revision's snapshot and running
discovery on it. The verdict rules:

- A verdict is a statement about revision content. Bad syntax, a reference to a
  revision module that does not exist, project code raising at import time, an
  unimportable declared path, a publication conflict, or a script file missing
  from the manifest all make the revision `invalid`, terminally.
- Environment trouble never produces a verdict. A missing external
  distribution, an unreachable backend, cache failure, or host I/O failure
  rolls the revision back to `materialized` and fails the job, so a retry gets
  a fair attempt. An import naming a module the revision itself ships is
  content, not environment, so writing `import helpers` where the tree holds
  `helpers.py` reports as an authoring mistake rather than retrying forever.
- An empty script file set is valid. A project whose revision declares no
  script files validates and can be activated, it simply offers no scripts.
- A revision whose script files all import cleanly and publish nothing is
  `invalid`. Each such script file is reported under the code
  `no_scripts_published`, and its Script File row reads `no_scripts` with the reason,
  which names the base class when the script file subclassed one of NetBox's own.
  One script file publishing nothing beside a working one leaves the verdict
  alone and is reported on its own row. A project whose only enabled script file
  publishes nothing does stop activating, so declare a helper file as a
  script file only alongside one that publishes.
- Stored validation errors are sanitized. Runtime namespaces, storage
  identities, and cache paths never appear in them or in job logs, module
  references read project-relative.

Validation also proves each published class is usable, not merely importable. It
builds the class's run form and resolves its fieldsets, so a variable Django
cannot turn into a field, a variable whose name the run form reserves, a fieldset
naming something that is not a variable, and a display name too long to record all
make the revision `invalid` instead of failing at the first attempt to run it.
What it learns is recorded on the revision as its [published Custom
Scripts](models/scriptprojectrevision.md).

Ownership, the validation lease, and why a crashed validation recovers by
itself are described on the [revision page](models/scriptprojectrevision.md).

## What activation does

Activation makes one validated revision the project's active revision and
publishes its [Scripts](models/netboxscript.md) as rows. Both happen in a
single database transaction, so a reader sees either the old revision with its old
scripts or the new revision with its new ones, never a mix.

Activation performs **no import**. It works entirely from what validation already
recorded, because re-importing could reach a different answer than the verdict the
revision carries, and a revision's meaning is fixed at its verdict.

Verifying the stored tree happens first and outside the transaction, since reading
and hashing every stored file can hold a conversation with a remote backend for a
while and the transaction that moves the pointer stays short. The project's
advisory lock covers both halves, so nothing restages or reclaims the tree between
proving it present and promoting it.

Re-activating the revision already in force is not a no-op. It synchronizes again,
which repairs rows that went missing, and because synchronization skips any row
that already matches, the repair writes nothing and logs nothing when nothing is
wrong. **Repair Scripts** on the Project's page is the operator's route to it,
and it reports how many rows moved so a repair does not read like a no-op.
Neither Activate route reaches it: both refuse the revision already in force,
at the route and not only by withholding the button. Where something is wrong,
each repaired row is a real row change: in a request it is recorded in the change
log and queues an update event attributed to whoever asked for it, and in a
background job it records neither, because a job applies no request processor.
That trail is what a recovery action most needs, so it is kept rather than
suppressed.

Deactivation is the reverse: it retires the revision, clears the project's
pointer, and retires every Script, because a project serving no revision
publishes nothing. It reads no storage and imports nothing, for the same reason.
Both operations lock the project row and then the revision row, in that order, so
a concurrent activation settles on one side or the other rather than interleaving.
