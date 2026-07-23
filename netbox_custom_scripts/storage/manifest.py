"""
Canonical manifest and digest computation for a project's source tree.

This module imports no Django ORM. It reads the configured storage limits. build_manifest
turns a mapping of source files into a sorted list of manifest entries plus a list of
first-class error records, so a bad upload becomes an inspectable invalid revision rather
than an exception. compute_digest turns the entries into a stable content address that is
identical across hosts.
"""

import hashlib
import json

from . import config
from .exceptions import LimitExceededError, UnsafePathError
from .paths import normalize_source_path


def build_manifest(files):
    """
    Return (entries, errors) for a mapping of source path to content bytes.

    Entries are {'path', 'size', 'sha256'} dicts sorted by path. Errors are
    {'path', 'code', 'message'} dicts, one per rejected file or tripped project limit. This
    function never raises for a content problem, so a caller can persist a failed upload as
    an invalid revision.
    """
    max_file_size = config.get_max_file_size()
    max_file_count = config.get_max_file_count()
    max_project_size = config.get_max_project_size()

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
                    'message': f'Multiple source files resolve to the path "{canonical}".',
                }
            )
            continue
        seen.add(canonical)
        try:
            entries.append(_build_entry(canonical, files[original_path], max_file_size))
        except LimitExceededError as exc:
            errors.append({'path': exc.path, 'code': exc.code, 'message': str(exc)})

    entries.sort(key=lambda entry: entry['path'])

    # The two project-wide limits are appended after the walk so a project that trips both
    # reports both, rather than stopping at whichever is detected first.
    if len(files) > max_file_count:
        errors.append(
            {
                'path': None,
                'code': 'too_many_files',
                'message': f'The project has {len(files)} files, over the {max_file_count} file limit.',
            }
        )
    candidate_total_size = sum(len(content) for content in files.values())
    if candidate_total_size > max_project_size:
        errors.append(
            {
                'path': None,
                'code': 'project_too_large',
                'message': f'The project totals {candidate_total_size} bytes, over the {max_project_size} byte limit.',
            }
        )

    return entries, errors


def _build_entry(canonical, content, max_file_size):
    size = len(content)
    if size > max_file_size:
        raise LimitExceededError(
            'file_too_large',
            f'"{canonical}" is {size} bytes, over the {max_file_size} byte limit.',
            path=canonical,
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
