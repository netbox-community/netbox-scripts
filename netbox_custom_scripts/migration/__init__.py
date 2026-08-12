"""
Migration tier for reading the built-in Custom Scripts feature.

This package is plugin-internal. source owns every read of the built-in implementation.
dialects decides a stored module's authoring dialect without importing it. plan turns that
into the Projects a migration would create and the findings an operator has to act on first.
staging creates those Projects and stages their content, activating nothing.
"""
