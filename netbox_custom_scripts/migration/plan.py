"""What a migration would produce, decided before anything is created."""

import hashlib
import posixpath
from dataclasses import asdict, dataclass

from django.core.exceptions import SuspiciousOperation, ValidationError
from django.utils.text import slugify

from ..choices import ProjectSourceTypeChoices
from ..compat import MIGRATION_HINTS
from ..utils import data_source_relative_path, source_path_to_dotted_name
from ..validators import data_paths_overlap
from . import dialects
from . import source as legacy_source

__all__ = (
    'BLOCKING',
    'READY',
    'WARNING',
    'ProposedProject',
    'build_report',
    'group',
)

READY = 'ready'
WARNING = 'warning'
BLOCKING = 'blocking'

_ROOT_NAME = 'the data source root'
_ROOT_STEM = 'data-source-root'

# key is a 100-character SlugField, and the digest keeps a truncated stem unique.
_KEY_DIGEST_LENGTH = 8
_KEY_STEM_LENGTH = 100 - _KEY_DIGEST_LENGTH - 1


@dataclass(frozen=True)
class ProposedProject:
    """One Project a migration would create, and the legacy modules it would hold."""

    key: str
    name: str
    source_type: str
    data_source_id: int | None
    data_path: str
    module_pks: tuple[int, ...]


def group(modules):
    """Return the Projects a set of legacy modules would migrate into, ordered by key."""
    buckets = {}
    for module in modules:
        buckets.setdefault(_identity(module), []).append(module)
    proposed = [_propose(identity, members) for identity, members in _collapse(buckets).items()]
    return sorted(proposed, key=lambda item: item.key)


def build_report(modules=None, read=None):
    """
    Return what a migration would do, changing nothing.

    Pass modules and read to report over supplied data rather than the live installation, which
    is how the rules are exercised without a database. A supplied set reports no references.
    """
    live = modules is None
    modules = legacy_source.legacy_modules() if live else modules
    read = read or legacy_source.read_source
    proposed = group(modules)
    # Grouped first: the path a module is staged at depends on which Project absorbed it.
    proposal_by_pk = {pk: proposal for proposal in proposed for pk in proposal.module_pks}
    counts = dict.fromkeys((dialects.NATIVE, dialects.LEGACY_IMPORT, dialects.REPORT_STYLE, dialects.UNPARSABLE), 0)
    entries = []
    findings = []
    for module in modules:
        dialect, module_findings = _inspect(module, read, proposal_by_pk[module.pk])
        counts[dialect] += 1
        entries.append(
            {
                'pk': module.pk,
                'path': _path(module),
                'file_root': module.file_root,
                'dialect': dialect,
                'scripts': [script.name for script in module.scripts],
            }
        )
        findings.extend(module_findings)
    reports = legacy_source.legacy_report_count() if live else 0
    if reports:
        findings.append(
            {
                'level': WARNING,
                'code': 'reports_excluded',
                'pk': None,
                'path': '',
                'message': (
                    f'{reports} built-in report module(s) are not part of this migration and are left '
                    f'untouched. Reports use an authoring API this plugin does not serve.'
                ),
            }
        )
    return {
        'status': _status(findings),
        'modules': entries,
        'reports': reports,
        'projects': [asdict(item) for item in proposed],
        'dialects': counts,
        'references': legacy_source.reference_counts() if live else {},
        'findings': findings,
    }


def _path(module):
    """Return the path to name a module by, preferring where it came from."""
    return module.data_path or module.file_path


def _staged_path(module, proposal):
    """Return the project-relative path a module's content is staged at, or None if it is outside."""
    # file_path is a basename for a synced module, so it is not what gets imported.
    if proposal.source_type == ProjectSourceTypeChoices.UPLOAD:
        return module.file_path
    return data_source_relative_path(module.data_path, proposal.data_path)


def _status(findings):
    """Return the worst level any finding carries."""
    levels = {finding['level'] for finding in findings}
    if BLOCKING in levels:
        return BLOCKING
    return WARNING if WARNING in levels else READY


def _identity(module):
    """Return the key a module groups on."""
    if module.data_source_id is None:
        return (ProjectSourceTypeChoices.UPLOAD, module.pk)
    return (ProjectSourceTypeChoices.DATA_SOURCE, module.data_source_id, posixpath.dirname(module.data_path))


def _collapse(buckets):
    """Merge each folder into the shallowest script-holding folder on its source that contains it."""
    # A project's tree already holds its subdirectories, so the deeper folder has nothing of its
    # own to stage, and the model refuses two overlapping data paths on one source anyway.
    folders = {}
    for identity in buckets:
        if identity[0] == ProjectSourceTypeChoices.DATA_SOURCE:
            folders.setdefault(identity[1], []).append(identity[2])
    merged = {}
    for identity, members in buckets.items():
        if identity[0] == ProjectSourceTypeChoices.DATA_SOURCE:
            identity = (identity[0], identity[1], _container(identity[2], folders[identity[1]]))
        merged.setdefault(identity, []).extend(members)
    return merged


def _container(folder, folders):
    """Return the shallowest folder in the set that contains this one, or the folder itself."""
    best, best_depth = folder, _depth(folder)
    for candidate in folders:
        depth = _depth(candidate)
        # Overlapping on strictly fewer segments can only mean the candidate is an ancestor.
        if depth < best_depth and data_paths_overlap(candidate, folder):
            best, best_depth = candidate, depth
    return best


def _depth(folder):
    """Return how many segments a folder path has."""
    return len(folder.split('/')) if folder else 0


def _propose(identity, members):
    """Return the Project one group of modules would become."""
    module_pks = tuple(sorted(member.pk for member in members))
    if identity[0] == ProjectSourceTypeChoices.UPLOAD:
        name = members[0].python_name
        return ProposedProject(
            key=_key(name, identity),
            name=name,
            source_type=ProjectSourceTypeChoices.UPLOAD,
            data_source_id=None,
            data_path='',
            module_pks=module_pks,
        )
    _, data_source_id, folder = identity
    return ProposedProject(
        key=_key(folder or _ROOT_STEM, identity),
        name=folder or _ROOT_NAME,
        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        data_source_id=data_source_id,
        data_path=folder,
        module_pks=module_pks,
    )


def _key(stem, identity):
    """Return a slug unique to one identity and short enough for the key field."""
    digest = hashlib.sha256('\0'.join(str(part) for part in identity).encode()).hexdigest()
    slug = slugify(stem.replace('/', '-'))[:_KEY_STEM_LENGTH] or 'project'
    return f'{slug}-{digest[:_KEY_DIGEST_LENGTH]}'


def _inspect(module, read, proposal):
    """Return one module's dialect and the findings it produces."""
    path = _path(module)
    try:
        body = read(module)
    except (OSError, SuspiciousOperation) as error:
        # One unreadable file must not deny an operator the rest of the report.
        message = f'{path} could not be read: {error}'
        return dialects.UNPARSABLE, [_finding(BLOCKING, 'source_unreadable', module, message)]

    findings = []
    staged = _staged_path(module, proposal)
    if staged is None:
        message = f'{path} sits outside the {proposal.name} project directory, so it cannot be staged.'
        findings.append(_finding(BLOCKING, 'not_importable', module, message))
    else:
        try:
            source_path_to_dotted_name(staged)
        except ValidationError as error:
            message = f'{path} cannot be imported as {staged}: {error.messages[0]}'
            findings.append(_finding(BLOCKING, 'not_importable', module, message))

    dialect = dialects.classify(body)
    if dialect == dialects.REPORT_STYLE:
        message = f'{path} is report-style. {MIGRATION_HINTS["extras.reports"]}'
        findings.append(_finding(BLOCKING, 'report_style', module, message))
    elif dialect == dialects.LEGACY_IMPORT:
        message = f'{path} imports the legacy authoring API. {MIGRATION_HINTS["extras.scripts"]}'
        findings.append(_finding(WARNING, 'legacy_import', module, message))
    elif dialect == dialects.UNPARSABLE:
        findings.append(_finding(BLOCKING, 'unparsable', module, f'{path} is not valid Python.'))
    return dialect, findings


def _finding(level, code, module, message):
    """Return one finding, carrying the module it belongs to."""
    return {'level': level, 'code': code, 'pk': module.pk, 'path': _path(module), 'message': message}
