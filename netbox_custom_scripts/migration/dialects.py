"""Authoring dialect of stored script source, decided by parsing rather than importing."""

import ast

from ..compat import LEGACY_MODULES

__all__ = (
    'LEGACY_IMPORT',
    'NATIVE',
    'REPORT_STYLE',
    'UNPARSABLE',
    'classify',
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
        methods = {child.name for child in node.body if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if 'run' not in methods and any(name.startswith('test_') for name in methods):
            return True
    return False
