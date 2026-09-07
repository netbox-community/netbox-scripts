"""
Migration tier for reading the built-in Custom Scripts feature.

This package is plugin-internal. source owns every read of the built-in implementation.
dialects decides a stored module's authoring dialect without importing it. plan turns that
into the Projects a migration would create and the findings an operator has to act on first.
staging creates those Projects and stages their content, activating nothing. mapping derives
which Script each built-in one becomes. cutover captures every reference the later
passes replay, freezes that mapping, and closes what a plugin can. references moves the
Event Rules, permissions, Job history and schedules. cleanup deletes what is left, and
verification reports whether any of it landed.
"""
