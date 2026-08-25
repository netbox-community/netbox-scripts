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
)

NATIVE = 'native'
LEGACY_IMPORT = 'legacy_import'
REPORT_STYLE = 'report_style'
UNPARSABLE = 'unparsable'

_REPORT_MODULE = 'extras.reports'
# The bare package counts: "import extras" reaches the authoring API without naming it.
_LEGACY_NAMES = frozenset({'extras', *LEGACY_MODULES})


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
    """Whether anything would publish from a module, given its built-in Script rows and its source."""
    # Two signals, because neither alone is complete: the rows miss a class published through
    # inheritance or script_order, and the source shape misses one the built-in feature recorded
    # but this plugin's dialect does not name.
    return any(script.is_executable for script in scripts) or defines_a_script(source)


def defines_a_script(source):
    """Return whether the source defines a class that could publish, refusing nothing."""
    # Shape rather than base name, because a class can subclass a base this file never names.
    # Unparsable counts as yes, so the file is declared and a verdict names it rather than it
    # being migrated as a helper in silence.
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return True
    return any(isinstance(node, ast.ClassDef) and 'run' in _method_names(node) for node in ast.walk(tree))


def _method_names(node):
    """Return the names of the methods one class declares."""
    return {child.name for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _imported_names(tree):
    """Return every module name the source imports, including the dotted form of a member import."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
            # "from extras import scripts" names only the package, so the member makes the pair.
            names.update(f'{node.module}.{alias.name}' for alias in node.names)
    return names


def _has_report_shape(tree):
    """Return whether any class declares test methods and no run method."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = _method_names(node)
        if 'run' not in methods and any(name.startswith('test_') for name in methods):
            return True
    return False
