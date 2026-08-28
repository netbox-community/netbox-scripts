"""
Canonical entrypoint snapshots for a project's revisions.

A revision freezes the enabled Module declarations it was staged under, so its validation
verdict keeps meaning when the live declarations change. build_entrypoint_snapshot turns
Module rows into the deterministic list a revision stores plus the digest that joins the
revision's identity. validate_entrypoint_snapshot covers the return trip, checking a
snapshot that has been persisted and read back before it becomes the authoritative input
to validation, activation, or execution.
"""

import hashlib
import json

from django.core.exceptions import ValidationError

from ..utils import source_path_to_dotted_name
from .exceptions import RevisionCorruptError, UnsafePathError
from .paths import case_insensitive_collisions, normalize_source_path

__all__ = (
    'EMPTY_SNAPSHOT_DIGEST',
    'build_entrypoint_snapshot',
    'compute_entrypoint_digest',
    'validate_entrypoint_snapshot',
)

# What compute_entrypoint_digest returns for an empty snapshot, the digest a revision
# staged with no enabled Modules carries. Also the model field default, so a row created
# without the service still describes itself.
EMPTY_SNAPSHOT_DIGEST = '4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945'

_ENTRY_KEYS = ('module', 'source_path')


def build_entrypoint_snapshot(modules):
    """
    Return (snapshot, digest) for an iterable of Module rows.

    The snapshot is a list of {'module', 'source_path'} dicts sorted by source path, and
    the digest is the sha256 over its canonical JSON form, computed like a manifest digest
    so the byte form is stable across hosts and across a JSONField round trip.
    """
    snapshot = sorted(
        ({'module': module.pk, 'source_path': module.source_path} for module in modules),
        key=lambda entry: entry['source_path'],
    )
    return snapshot, compute_entrypoint_digest(snapshot)


def compute_entrypoint_digest(snapshot):
    """Return the 64 character lowercase hex sha256 that addresses one snapshot."""
    payload = json.dumps(snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def validate_entrypoint_snapshot(snapshot, entrypoint_digest):
    """
    Check a persisted snapshot before it becomes authoritative input again.

    A snapshot read back from a database row decides which files validation imports and
    execution loads, so every entry is confirmed to be well formed, canonically pathed,
    importable, unique, and free of case-insensitive collisions, and the list must be sorted
    and still be the one its digest addresses. Two paths that import under one module name are
    rejected too, because only one of them could ever execute. Raises RevisionCorruptError
    listing every problem found. Returns the entries as a tuple.
    """
    if not isinstance(snapshot, list):
        raise RevisionCorruptError('The stored entrypoint snapshot is not a list.', ['snapshot_not_a_list'])

    reasons = []
    module_ids = set()
    paths = set()
    claimants_by_name = {}
    previous_path = None
    for index, entry in enumerate(snapshot):
        entry_reasons = _validate_entry(index, entry, module_ids, paths, claimants_by_name)
        reasons.extend(entry_reasons)
        if entry_reasons:
            continue
        path = entry['source_path']
        if previous_path is not None and path < previous_path:
            reasons.append(f'unsorted_entry:{path}')
        previous_path = path

    reasons.extend(f'case_fold_conflict:{path}' for path in sorted(case_insensitive_collisions(paths)))
    for dotted in sorted(claimants_by_name):
        claimants = claimants_by_name[dotted]
        if len(claimants) > 1:
            reasons.extend(f'duplicate_module_name:{path}' for path in sorted(claimants))

    # The digest is computed from the entries, so it runs only once they are known sound.
    if not reasons and compute_entrypoint_digest(snapshot) != entrypoint_digest:
        reasons.append('entrypoint_digest_mismatch')

    if reasons:
        raise RevisionCorruptError('The stored entrypoint snapshot cannot be trusted.', reasons)
    return tuple(snapshot)


def _validate_entry(index, entry, module_ids, paths, claimants_by_name):
    """Return the reasons one persisted entry cannot be used, recording what it claims."""
    if not isinstance(entry, dict):
        return [f'malformed_entry:{index}']
    missing = [key for key in _ENTRY_KEYS if key not in entry]
    if missing:
        return [f'missing_fields:{index}:{",".join(missing)}']
    unexpected = sorted(set(entry) - set(_ENTRY_KEYS))
    if unexpected:
        return [f'unexpected_fields:{index}:{",".join(unexpected)}']

    reasons = []
    module_id = entry['module']
    if isinstance(module_id, bool) or not isinstance(module_id, int) or module_id < 1:
        reasons.append(f'malformed_module_id:{index}')
    else:
        if module_id in module_ids:
            reasons.append(f'duplicate_module:{module_id}')
        module_ids.add(module_id)

    path = entry['source_path']
    if not isinstance(path, str):
        reasons.append(f'malformed_path:{index}')
        return reasons
    try:
        canonical = normalize_source_path(path)
    except UnsafePathError as error:
        reasons.append(f'{error.code}:{path}')
        return reasons
    if canonical != path:
        reasons.append(f'non_canonical_path:{path}')
        return reasons
    try:
        dotted = source_path_to_dotted_name(path)
    except ValidationError:
        reasons.append(f'unimportable_path:{path}')
    else:
        claimants_by_name.setdefault(dotted, set()).add(path)
    if path in paths:
        reasons.append(f'duplicate_path:{path}')
    paths.add(path)
    return reasons
