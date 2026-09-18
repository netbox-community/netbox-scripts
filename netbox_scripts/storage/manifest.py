"""
Canonical manifest and digest computation for a project's source tree.

build_manifest turns a mapping of source files into a sorted list of manifest entries plus
a list of first-class error records, so a bad upload becomes an inspectable invalid
revision rather than an exception. compute_digest turns the entries into a stable content
address that is identical across hosts. validate_manifest covers the return trip, checking
a manifest that has been persisted and read back before anything acts on it.
"""

import hashlib
import json
import re

from django.utils.translation import gettext_lazy as _

from .exceptions import LimitExceededError, RevisionCorruptError, UnsafePathError
from .paths import case_insensitive_collisions, normalize_source_path

__all__ = (
    'build_manifest',
    'compute_digest',
    'validate_manifest',
)

_SHA256 = re.compile(r'[0-9a-f]{64}')


def build_manifest(files, limits):
    """
    Return (entries, errors) for a mapping of source path to content bytes.

    Entries are {'path', 'size', 'sha256'} dicts sorted by path. Errors are
    {'path', 'code', 'message'} dicts, one per rejected file, path conflict, or tripped
    project limit. The supplied limits apply to every check, so one operation cannot see two
    different limits. This function never raises for a content problem, so a caller can
    persist a failed upload as an invalid revision.
    """
    max_file_size = limits.max_file_size
    max_file_count = limits.max_file_count
    max_project_size = limits.max_project_size

    entries = []
    errors = []
    seen = set()

    for original_path in sorted(files):
        try:
            canonical = normalize_source_path(original_path)
        except UnsafePathError as exc:
            errors.append({'path': exc.path, 'code': exc.code, 'message': str(exc)})
            continue
        if canonical in seen:
            errors.append(
                {
                    'path': original_path,
                    'code': 'duplicate_path',
                    'message': _('Multiple source files resolve to the path "{path}".').format(path=canonical),
                }
            )
            continue
        seen.add(canonical)
        try:
            entries.append(_build_entry(canonical, files[original_path], max_file_size))
        except LimitExceededError as exc:
            errors.append({'path': exc.path, 'code': exc.code, 'message': str(exc)})

    entries, conflicts = _reject_path_conflicts(entries)
    errors.extend(conflicts)

    entries.sort(key=lambda entry: entry['path'])

    # The two project-wide limits are appended after the walk so a project that trips both
    # reports both, rather than stopping at whichever is detected first.
    if len(files) > max_file_count:
        errors.append(
            {
                'path': None,
                'code': 'too_many_files',
                'message': _('The project has {count} files, over the {limit} file limit.').format(
                    count=len(files), limit=max_file_count
                ),
            }
        )
    candidate_total_size = sum(len(content) for content in files.values())
    if candidate_total_size > max_project_size:
        errors.append(
            {
                'path': None,
                'code': 'project_too_large',
                'message': _('The project totals {size} bytes, over the {limit} byte limit.').format(
                    size=candidate_total_size, limit=max_project_size
                ),
            }
        )

    return entries, errors


def _reject_path_conflicts(entries):
    """
    Return (kept_entries, errors) with every entry whose path cannot coexist in one tree.

    A source tree cannot hold both a file and a directory at one path, so "pkg" and
    "pkg/module.py" cannot both be stored. The writer would fail on whichever arrives
    second, and which one that is depends on mapping order, so the conflict is rejected
    here as content rather than surfacing later as a filesystem error. Distinct names
    sharing one case-insensitive form are rejected the same way, because the path policy
    promises accepted paths stay materializable on every supported host and hosts such
    as APFS compare names without case.
    """
    files = {entry['path'] for entry in entries}
    kept = []
    errors = []
    for entry in entries:
        segments = entry['path'].split('/')
        ancestors = ['/'.join(segments[:index]) for index in range(1, len(segments))]
        blocking = next((ancestor for ancestor in ancestors if ancestor in files), None)
        if blocking is None:
            kept.append(entry)
            continue
        errors.append(
            {
                'path': entry['path'],
                'code': 'path_conflict',
                'message': _('"{blocking}" is a file and cannot also contain another file.').format(blocking=blocking),
            }
        )

    collisions = case_insensitive_collisions(entry['path'] for entry in kept)
    remaining = []
    for entry in kept:
        pair = collisions.get(entry['path'])
        if pair is None:
            remaining.append(entry)
            continue
        node, other = pair
        errors.append(
            {
                'path': entry['path'],
                'code': 'case_fold_conflict',
                'message': _('"{node}" and "{other}" collide when letter case is ignored.').format(
                    node=node, other=other
                ),
            }
        )
    return remaining, errors


def _build_entry(canonical, content, max_file_size):
    size = len(content)
    if size > max_file_size:
        raise LimitExceededError(
            path=canonical,
            code='file_too_large',
            message=_('"{path}" is {size} bytes, over the {limit} byte limit.').format(
                path=canonical, size=size, limit=max_file_size
            ),
        )
    return {'path': canonical, 'size': size, 'sha256': hashlib.sha256(content).hexdigest()}


def compute_digest(entries):
    """
    Return the 64 character lowercase hex sha256 that content-addresses manifest entries.

    Entries are sorted by path and serialized with sorted object keys and a fixed separator
    so the byte form is stable across hosts and across a JSONField round trip, where only
    array element order is preserved and object key order is not.

    This digest addresses VALID entries only. The revision service must not call it, nor
    enforce a (project, digest) identity, when build_manifest reported errors: two different
    invalid trees can share the same accepted subset and would otherwise collide. Invalid
    staging attempts are stored with a null digest and are not content-deduplicated.
    """
    canonical = sorted(entries, key=lambda entry: entry['path'])
    payload = json.dumps(canonical, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def validate_manifest(manifest, digest=None):
    """
    Check a persisted manifest before any path from it reaches an open call.

    build_manifest owns construction and reports content problems as error records. This
    function owns the return trip: a manifest read back from a database row is input again,
    so every entry is confirmed to carry a canonical path with a plausible size and checksum
    before the storage layer walks it. Passing the revision digest also confirms the manifest
    is the one that digest addresses. Raises RevisionCorruptError listing every problem
    found.
    """
    if not isinstance(manifest, list):
        raise RevisionCorruptError(_('The stored manifest is not a list.'), ['manifest_not_a_list'])

    reasons = []
    paths = set()
    for index, entry in enumerate(manifest):
        reasons.extend(_validate_entry(index, entry, paths))
    reasons.extend(_conflicting_paths(paths))

    # compute_digest reads entry['path'], so it runs only once the entries are known sound.
    if digest is not None and not reasons and compute_digest(manifest) != digest:
        reasons.append('digest_mismatch')

    if reasons:
        raise RevisionCorruptError(_('The stored manifest cannot be trusted.'), reasons)


def _validate_entry(index, entry, paths):
    """Return the reasons one persisted entry cannot be used, recording the path it claims."""
    if not isinstance(entry, dict):
        return [f'malformed_entry:{index}']
    missing = [field for field in ('path', 'size', 'sha256') if field not in entry]
    if missing:
        return [f'missing_fields:{index}:{",".join(missing)}']

    path = entry['path']
    if not isinstance(path, str):
        return [f'malformed_path:{index}']
    try:
        canonical = normalize_source_path(path)
    except UnsafePathError as error:
        # This is what stops a stored "../outside.py" walking out of the revision directory:
        # ".." is a real directory entry, so opening it with O_NOFOLLOW succeeds. The
        # rejection code is passed through rather than flattened, so a stored path that no
        # host can materialize names its own problem instead of arriving as a generic refusal.
        return [f'{error.code}:{path}']
    if canonical != path:
        return [f'non_canonical_path:{path}']

    reasons = []
    if path in paths:
        reasons.append(f'duplicate_path:{path}')
    paths.add(path)

    size = entry['size']
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        reasons.append(f'malformed_size:{path}')
    checksum = entry['sha256']
    if not isinstance(checksum, str) or not _SHA256.fullmatch(checksum):
        reasons.append(f'malformed_checksum:{path}')
    return reasons


def _conflicting_paths(paths):
    """Return a reason for every stored path that cannot coexist with the others in one tree."""
    conflicts = []
    for path in sorted(paths):
        segments = path.split('/')
        ancestors = ('/'.join(segments[:index]) for index in range(1, len(segments)))
        if any(ancestor in paths for ancestor in ancestors):
            conflicts.append(f'path_conflict:{path}')
    conflicts.extend(f'case_fold_conflict:{path}' for path in sorted(case_insensitive_collisions(paths)))
    return conflicts
