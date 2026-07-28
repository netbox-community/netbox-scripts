"""
Runtime tier for executing stored revisions.

This package is plugin-internal. cache owns the disposable on-disk tree a revision is
imported from: pods are immutable and horizontally scaled, so the tree is regenerated from
the authoritative store and verified against the revision manifest before anything trusts
it, never trusted because it exists. exceptions owns the errors this tier raises.
"""
