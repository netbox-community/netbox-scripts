"""
Enforce the platform contract that NetBox Cloud and NetBox Enterprise impose on this plugin.

Both platforms run the plugin as immutable, horizontally scaled Kubernetes pods. Three
consequences drive everything below. A pod's filesystem is private to that pod and is
discarded when it restarts, so bytes written outside a Django storage backend are invisible
to the next request and to every other pod. A pod can be replaced at any moment, so work
held in a thread or an in-process timer is lost rather than finished. And no operator can
reach into a pod to run something by hand, so anything that only a management command can
do cannot be done at all.

The check walks each module's syntax tree rather than its text. That distinction earns its
keep twice over: prose describing a forbidden call, which this file and the storage
documentation are both full of, cannot trip it, and a call reached through an alias is
still recognised, so `import shutil as sh` followed by `sh.rmtree(...)` is caught where a
search for "shutil.rmtree" would miss it.

Code that must breach the contract by design, such as the runtime script cache that has to
materialize a revision on local disk before importing it, carries the marker
`cloud-compat: ok` on the offending line or anywhere in the statement spanning it, together
with a comment saying why.

Usage: `python scripts/check_cloud_compat.py`, exiting non-zero on any finding. The
contract is written up in AGENTS.md under "Cloud and Enterprise compatibility".
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
PACKAGE = REPOSITORY / 'netbox_custom_scripts'

# Generated, vendored, or deliberately host-bound code the contract does not govern.
SKIP_DIRECTORIES = frozenset({'__pycache__', 'migrations', 'tests'})

WAIVER = 'cloud-compat: ok'

EPHEMERAL_DISK = (
    'writes to the pod filesystem, which is private to one pod and discarded on restart. '
    'Route it through the storage backend that storage.config.get_storage() returns.'
)
LOST_ON_RESTART = (
    'holds work inside the process, which a pod replacement discards mid-flight. '
    "Hand the work to NetBox's JobRunner instead."
)
NOT_SHARED = 'keeps state that one pod cannot share with the others. Use the platform Redis.'

FORBIDDEN_CALLS = {
    'os.mkdir': EPHEMERAL_DISK,
    'os.makedirs': EPHEMERAL_DISK,
    'os.rmdir': EPHEMERAL_DISK,
    'os.remove': EPHEMERAL_DISK,
    'os.unlink': EPHEMERAL_DISK,
    'os.rename': EPHEMERAL_DISK,
    'os.replace': EPHEMERAL_DISK,
    'os.link': EPHEMERAL_DISK,
    'os.symlink': EPHEMERAL_DISK,
    'os.truncate': EPHEMERAL_DISK,
    'os.chmod': EPHEMERAL_DISK,
    'os.chown': EPHEMERAL_DISK,
    'os.utime': EPHEMERAL_DISK,
    'shutil.copy': EPHEMERAL_DISK,
    'shutil.copy2': EPHEMERAL_DISK,
    'shutil.copyfile': EPHEMERAL_DISK,
    'shutil.copytree': EPHEMERAL_DISK,
    'shutil.move': EPHEMERAL_DISK,
    'shutil.rmtree': EPHEMERAL_DISK,
    'shutil.make_archive': EPHEMERAL_DISK,
    'shutil.unpack_archive': EPHEMERAL_DISK,
    'threading.Thread': LOST_ON_RESTART,
    'threading.Timer': LOST_ON_RESTART,
    'asyncio.run': LOST_ON_RESTART,
    'subprocess.run': (
        'shells out to tooling the container image is not guaranteed to carry, and to a '
        'filesystem the next request will not see. Do the work in Python or in a job.'
    ),
    'socket.bind': 'opens a listener that the Kubernetes ingress layer already owns.',
    'socket.listen': 'opens a listener that the Kubernetes ingress layer already owns.',
}
FORBIDDEN_CALLS['subprocess.Popen'] = FORBIDDEN_CALLS['subprocess.run']
FORBIDDEN_CALLS['subprocess.call'] = FORBIDDEN_CALLS['subprocess.run']
FORBIDDEN_CALLS['subprocess.check_call'] = FORBIDDEN_CALLS['subprocess.run']
FORBIDDEN_CALLS['subprocess.check_output'] = FORBIDDEN_CALLS['subprocess.run']

FORBIDDEN_IMPORTS = {
    'tempfile': (
        'creates files on the pod filesystem, which the next request will not see. '
        'Stage content in memory and write it through the storage backend.'
    ),
    'multiprocessing': LOST_ON_RESTART,
    'sched': LOST_ON_RESTART,
    'apscheduler': LOST_ON_RESTART,
}

# Method names that write no matter what object carries them. Every os.* equivalent below is
# forbidden, so the pathlib spelling of the same operation is too. Names an unrelated object
# plausibly carries stay out: .open has the mode rule instead, and .replace is str.replace far
# more often than it is Path.replace.
FORBIDDEN_METHODS = (
    'chmod',
    'hardlink_to',
    'lchmod',
    'mkdir',
    'rename',
    'rmdir',
    'symlink_to',
    'touch',
    'unlink',
    'write_bytes',
    'write_text',
)

FORBIDDEN_SETTINGS = ('MEDIA_ROOT', 'STATIC_ROOT')

FORBIDDEN_PATH_PREFIXES = ('/tmp/', '/var/', '/opt/netbox', '/home/', '/root/', '/etc/netbox')

FORBIDDEN_CACHE_BACKENDS = ('locmem', 'filebased')

MANAGEMENT_COMMAND_BASE = 'BaseCommand'

WRITING_MODES = 'wax+'


class Finding:
    """One breach of the contract, with the source line that caused it."""

    def __init__(self, module, line, subject, consequence):
        self.module = module
        self.line = line
        self.subject = subject
        self.consequence = consequence

    def sort_key(self):
        """Return the ordering that groups findings by module and follows the source."""
        return (str(self.module), self.line)


class ContractVisitor(ast.NodeVisitor):
    """
    Collect every breach of the platform contract in one module.

    Import statements are recorded as they are visited so that a later call can be resolved
    through its aliases, which is why one visitor instance covers one whole module.
    """

    def __init__(self, module, lines, docstrings):
        self.module = module
        self.lines = lines
        self.docstrings = docstrings
        self.imports = {}
        self.findings = []

    def visit_Import(self, node):
        """Record what a plain import binds, and refuse a module the contract forbids."""
        for alias in node.names:
            self.imports[alias.asname or alias.name.split('.')[0]] = alias.name
            self._check_module(alias.name, node)
        self.generic_visit(node)

    def visit_ImportFrom(self, node):
        """Record what a from-import binds, so a bare name still resolves to its module."""
        if node.module and not node.level:
            for alias in node.names:
                self.imports[alias.asname or alias.name] = f'{node.module}.{alias.name}'
            self._check_module(node.module, node)
        self.generic_visit(node)

    def _check_module(self, module, node):
        """Record a finding when a module is forbidden outright, including a submodule of one."""
        root = module.split('.')[0]
        if root in FORBIDDEN_IMPORTS:
            self._record(node, f'import {module}', FORBIDDEN_IMPORTS[root])

    def visit_Call(self, node):
        """Refuse a call that reaches the pod filesystem, a thread, or the shell."""
        target = self._resolve(node.func)
        if target in FORBIDDEN_CALLS:
            self._record(node, f'{target}()', FORBIDDEN_CALLS[target])
        elif target == 'os.open':
            self._check_os_open(node, target)
        elif target == 'open' and self._opens_for_writing(node):
            self._record(node, 'open() in a writing mode', EPHEMERAL_DISK)
        elif isinstance(node.func, ast.Attribute) and target is None:
            name = node.func.attr
            if name in FORBIDDEN_METHODS:
                self._record(node, f'.{name}()', EPHEMERAL_DISK)
            elif name == 'open' and self._opens_for_writing(node) and not self._is_storage_call(node):
                self._record(node, '.open() in a writing mode', EPHEMERAL_DISK)
        self.generic_visit(node)

    def _check_os_open(self, node, target):
        """Record os.open only when its flags create or truncate, since it also opens for reading."""
        flags = {'O_CREAT', 'O_TRUNC', 'O_WRONLY', 'O_RDWR', 'O_APPEND'}
        names = {leaf.attr for leaf in ast.walk(node) if isinstance(leaf, ast.Attribute)}
        names.update(leaf.id for leaf in ast.walk(node) if isinstance(leaf, ast.Name))
        if any(flag in names for flag in flags):
            self._record(node, 'os.open() with a creating or writing flag', EPHEMERAL_DISK)

    def visit_ClassDef(self, node):
        """Refuse a management command, which neither platform can invoke."""
        for base in node.bases:
            resolved = self._resolve(base) or ''
            if resolved.split('.')[-1] == MANAGEMENT_COMMAND_BASE:
                self._record(
                    node,
                    f'class {node.name}',
                    'is a management command, which no operator can invoke on either platform. '
                    'Move the work into a data migration or a job, or waive it here if it only '
                    'duplicates a route every platform already has.',
                )
        self.generic_visit(node)

    def visit_Name(self, node):
        """Refuse a setting that resolves to a location inside one pod."""
        if node.id in FORBIDDEN_SETTINGS:
            self._record(node, node.id, 'names a directory inside one pod. Address content by storage key instead.')
        self.generic_visit(node)

    def visit_Attribute(self, node):
        """Refuse a per-pod setting reached as an attribute, such as settings.MEDIA_ROOT."""
        if node.attr in FORBIDDEN_SETTINGS:
            self._record(node, node.attr, 'names a directory inside one pod. Address content by storage key instead.')
        self.generic_visit(node)

    def visit_Constant(self, node):
        """Flag a spelled-out host path or a per-pod cache backend, ignoring documentation."""
        if id(node) in self.docstrings or not isinstance(node.value, str):
            return
        for prefix in FORBIDDEN_PATH_PREFIXES:
            if node.value.startswith(prefix):
                self._record(
                    node,
                    f'the path "{node.value}"',
                    'is a host location that the container image either lacks or will not let this process write.',
                )
                return
        if any(f'backends.{backend}' in node.value for backend in FORBIDDEN_CACHE_BACKENDS):
            self._record(node, f'the cache backend "{node.value}"', NOT_SHARED)

    def _resolve(self, node):
        """
        Return the canonical dotted path a name or attribute chain refers to, or None.

        The leading component is translated through this module's imports, so shutil.rmtree
        reached as sh.rmtree resolves to the same thing, and a name imported directly with
        "from os import mkdir" resolves to os.mkdir rather than to a bare mkdir.
        """
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if not isinstance(node, ast.Name):
            return None
        head = self.imports.get(node.id, node.id if not parts else None)
        if head is None:
            return None
        parts.append(head)
        return '.'.join(reversed(parts))

    def _opens_for_writing(self, node):
        """Return whether an open call names a mode that creates or modifies its target."""
        # The builtin takes (file, mode) while a method open takes (mode) first.
        position = 0 if isinstance(node.func, ast.Attribute) else 1
        mode = None
        if len(node.args) > position and isinstance(node.args[position], ast.Constant):
            mode = node.args[position].value
        for keyword in node.keywords:
            if keyword.arg == 'mode' and isinstance(keyword.value, ast.Constant):
                mode = keyword.value.value
        return isinstance(mode, str) and any(flag in mode for flag in WRITING_MODES)

    def _is_storage_call(self, node):
        """
        Return whether an open call is a Django storage backend's own open.

        Writing through a backend is the sanctioned path, so a receiver named for one is not
        a finding. The name is the only signal available without running the code, which is
        why the accepted spellings are kept few and explicit.
        """
        receiver = node.func.value
        while isinstance(receiver, (ast.Attribute, ast.Subscript, ast.Call)):
            receiver = getattr(receiver, 'value', None) or getattr(receiver, 'func', None)
        names = frozenset({'default_storage', 'storage', 'storages'})
        return isinstance(receiver, ast.Name) and receiver.id in names

    def _record(self, node, subject, consequence):
        if self._waived(node, getattr(node, 'lineno', 1)):
            return
        self.findings.append(Finding(self.module, node.lineno, subject, consequence))

    def _waived(self, node, line):
        """Return whether the marker appears anywhere in the statement this node belongs to."""
        end = getattr(node, 'end_lineno', line)
        return any(WAIVER in self.lines[index - 1] for index in range(line, end + 1) if index <= len(self.lines))


def collect_docstrings(tree):
    """Return the ids of every docstring node, so prose about a pattern is not a finding."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                found.add(id(body[0].value))
    return found


def modules_under(root):
    """Yield every module the contract governs, in a stable order."""
    for path in sorted(root.rglob('*.py')):
        if not SKIP_DIRECTORIES.intersection(path.relative_to(root).parts):
            yield path


def inspect(path):
    """Return the findings in one module, or an empty list when it cannot be parsed."""
    try:
        source = path.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError) as error:
        print(f'cloud-compat: could not read {path}: {error}', file=sys.stderr)
        return []
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        # A module that does not parse is the linter's finding, not this contract's.
        return []
    visitor = ContractVisitor(path.relative_to(REPOSITORY), source.splitlines(), collect_docstrings(tree))
    visitor.visit(tree)
    return visitor.findings


def report(findings):
    """Print every finding grouped by module, and return the process exit status."""
    if not findings:
        print('cloud-compat: OK, the package holds nothing that breaks on Cloud or Enterprise.')
        return 0
    print('cloud-compat: FAIL.')
    print('NetBox Cloud and NetBox Enterprise run this plugin as immutable, horizontally scaled')
    print('Kubernetes pods. Each line below behaves differently there than on a single VM.')
    print('See AGENTS.md, section "Cloud and Enterprise compatibility".')
    current = None
    for finding in sorted(findings, key=Finding.sort_key):
        if finding.module != current:
            current = finding.module
            print(f'\n{current}')
        print(f'  line {finding.line}: {finding.subject} {finding.consequence}')
    print(f'\n{len(findings)} finding{"s" if len(findings) != 1 else ""}.')
    return 1


def main():
    """Inspect every governed module and report what it found."""
    findings = [finding for module in modules_under(PACKAGE) for finding in inspect(module)]
    return report(findings)


if __name__ == '__main__':
    sys.exit(main())
