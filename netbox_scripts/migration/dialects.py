"""Authoring dialect of stored script source, decided by parsing rather than importing."""

import ast

from ..compat import LEGACY_MODULES

__all__ = (
    'LEGACY_IMPORT',
    'NATIVE',
    'REPORT_STYLE',
    'UNPARSABLE',
    'classify',
    'defines_a_script',
    'publishes',
    'reachable_nodes',
)

NATIVE = 'native'
LEGACY_IMPORT = 'legacy_import'
REPORT_STYLE = 'report_style'
UNPARSABLE = 'unparsable'

_REPORT_MODULE = 'extras.reports'
# The bare package counts: "import extras" reaches the authoring API without naming it.
_LEGACY_NAMES = frozenset({'extras', *LEGACY_MODULES})

# An unseen base may carry run, which keeps a class out of the report verdict. Not these two: a
# subclass of Report is a report whatever it inherits, and object carries nothing.
_BASES_WITHOUT_RUN = frozenset({'Report', 'object'})
# A subclass of either could publish without declaring run of its own.
_SCRIPT_BASES = frozenset({'BaseScript', 'Script'})
_TYPE_CHECKING = 'TYPE_CHECKING'


def classify(source):
    """Return one module's authoring dialect: NATIVE, LEGACY_IMPORT, REPORT_STYLE or UNPARSABLE."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return UNPARSABLE
    imported = _imported_names(tree)
    # Report shape wins, because it needs a rewrite rather than the legacy bucket's one-line edit.
    if _REPORT_MODULE in imported or _has_report_shape(tree):
        return REPORT_STYLE
    if imported & _LEGACY_NAMES:
        return LEGACY_IMPORT
    return NATIVE


def publishes(scripts, source):
    """Whether anything would publish from a module, given its built-in Custom Script rows and its source."""
    # Two signals, because neither alone is complete: the rows miss a class published through
    # inheritance or script_order, and the source shape misses one the built-in feature recorded
    # but this plugin's dialect does not name.
    return any(script.is_executable for script in scripts) or defines_a_script(source)


def defines_a_script(source):
    """Return whether the source defines a class that could publish, refusing nothing."""
    # Shape first, because a class can subclass a base this file never names, then the two base
    # names that publish with no run method of their own. Unparsable counts as yes, so the file is
    # declared and a verdict names it rather than it being migrated as a helper in silence.
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    # A local base carrying run is enumerated here itself, so the bases need no walk.
    return any(
        'run' in _method_names(node) or not _SCRIPT_BASES.isdisjoint(_base_names(node)) for node in _classes(tree)
    )


def _classes(tree):
    """Return every class the module declares, nested ones included."""
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


def _class_map(tree):
    """Return the module's classes by name for resolving a base, the last declaration of a name winning."""
    return {node.name: (_method_names(node), _base_names(node)) for node in _classes(tree)}


def _method_names(node):
    """Return the names of the methods one class declares."""
    return {child.name for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _base_names(node):
    """Return the bare name of each base one class declares, dropping any dotted prefix."""
    return [
        base.id if isinstance(base, ast.Name) else base.attr
        for base in node.bases
        if isinstance(base, (ast.Name, ast.Attribute))
    ]


def _bases_supply_run(bases, classes, seen=()):
    """Return whether a class's bases leave room for an inherited run method."""
    for name in bases:
        if name in seen:
            continue
        if name in classes:
            methods, inherited = classes[name]
            if 'run' in methods or _bases_supply_run(inherited, classes, (*seen, name)):
                return True
        elif name not in _BASES_WITHOUT_RUN:
            return True
    return False


def _imported_names(tree):
    """Return every module name the source imports, including the dotted form of a member import."""
    names = set()
    # Function bodies count here, unlike the import-time walk plan.py asks for: a deferred import
    # of the legacy authoring API still runs when the function is called.
    for node, _conditional in reachable_nodes(tree, skip_functions=False):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            # "from extras import scripts" names only the package, so the member makes the pair.
            names.update(f'{node.module}.{alias.name}' for alias in node.names)
    return names


def reachable_nodes(node, *, skip_functions, conditional=False):
    """
    Yield each node the module could evaluate, paired with whether a condition decides it.

    A branch guarded by TYPE_CHECKING or by a constant that is always false is skipped, and its
    else branch is yielded as unconditional. skip_functions drops function and lambda bodies,
    which is the difference between asking what a module evaluates as it imports and asking what
    it could ever evaluate.
    """
    yield from _walk(ast.iter_child_nodes(node), skip_functions=skip_functions, conditional=conditional)


def _walk(statements, *, skip_functions, conditional):
    # A selected else branch is walked through this same pass, so a function body, a nested guard
    # and a nested condition are ruled on there too.
    for child in statements:
        if skip_functions and isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
            continue
        yield child, conditional
        test = child.test if isinstance(child, ast.If) else None
        # TYPE_CHECKING is false at runtime by definition, whatever name the module bound it to. The
        # third test takes a literal and never an expression, because reading an operator's source
        # never means evaluating it, so a condition only their host can settle stays walked.
        if (
            (isinstance(test, ast.Name) and test.id == _TYPE_CHECKING)
            or (isinstance(test, ast.Attribute) and test.attr == _TYPE_CHECKING)
            or (isinstance(test, ast.Constant) and not test.value)
        ):
            # The guard settles which branch runs, so its alternative depends on nothing further.
            yield from _walk(child.orelse, skip_functions=skip_functions, conditional=conditional)
            continue
        yield from _walk(
            ast.iter_child_nodes(child), skip_functions=skip_functions, conditional=conditional or test is not None
        )


def _has_report_shape(tree):
    """Return whether any class declares test methods and could not have inherited a run method."""
    classes = _class_map(tree)
    for node in _classes(tree):
        methods = _method_names(node)
        if 'run' in methods or not any(name.startswith('test_') for name in methods):
            continue
        if not _bases_supply_run(_base_names(node), classes):
            return True
    return False
