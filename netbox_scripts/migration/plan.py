"""What a migration would produce, decided before anything is created."""

import ast
import hashlib
import importlib.util
import posixpath
import sys
from dataclasses import asdict, dataclass

from django.core.exceptions import SuspiciousOperation, ValidationError
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ..compat import MIGRATION_HINTS
from ..ingestion import uploaded_source_path
from ..models import ScriptProject
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
    'existing_project',
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

    def source_path_for(self, module):
        """Return the module's path inside this proposal, or None when it is outside."""
        # Both branches call what staging calls, so this cannot propose a path ingestion refuses.
        # file_path is a basename for a synced module, which is why that branch uses data_path.
        if self.source_type == ProjectSourceTypeChoices.UPLOAD:
            return uploaded_source_path(module.file_path)
        return data_source_relative_path(module.data_path, self.data_path)


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
    # The path a module is staged at depends on which Project absorbed it.
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
    findings.extend(_root_findings(proposed))
    if live:
        findings.extend(_existing_project_findings(proposed))
    reports = legacy_source.legacy_report_count() if live else 0
    if reports:
        findings.append(
            {
                'level': WARNING,
                'code': 'reports_excluded',
                'pk': None,
                'path': '',
                'message': _(
                    '{reports} built-in report module(s) are not part of this migration. Reports use an '
                    'authoring API this plugin does not serve, so nothing moves them onto it. The cutover '
                    'still withdraws any permission, and disables any Event Rule, naming extras.script or '
                    'extras.scriptmodule, object types that serve reports too, and the repointing pass '
                    'then drops that coverage for good.'
                ).format(reports=reports),
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


def _root_findings(proposed):
    """Return a blocking finding for each proposal that resolves to a data source root."""
    # The model refuses an empty data path, so staging one would raise rather than report.
    findings = []
    for proposal in proposed:
        if proposal.source_type != ProjectSourceTypeChoices.DATA_SOURCE or proposal.data_path:
            continue
        findings.append(
            {
                'level': BLOCKING,
                'code': 'data_source_root',
                'pk': None,
                'path': '',
                'message': _(
                    '{count} module(s) collapse to the root of a data source, so '
                    'project "{key}" would take the whole source as its tree. Move those '
                    'scripts under a directory on the source, then run the inventory again.'
                ).format(count=len(proposal.module_pks), key=proposal.key),
            }
        )
    return findings


def _existing_project_findings(proposed):
    """Return a blocking finding for each proposal an existing Script Project collides with."""
    findings = []
    for proposal in proposed:
        for project in _colliding_projects(proposal):
            if project.data_path == proposal.data_path or proposal.source_type == ProjectSourceTypeChoices.UPLOAD:
                if project.activation_policy != ActivationPolicyChoices.MANUAL:
                    findings.append(_not_manual_finding(proposal, project))
            else:
                findings.append(_conflict_finding(proposal, project))
    return findings


def _colliding_projects(proposal):
    """Return the existing Projects staging would reuse or be refused by for one proposal."""
    if proposal.source_type == ProjectSourceTypeChoices.UPLOAD:
        return ScriptProject.objects.filter(key=proposal.key)
    siblings = ScriptProject.objects.filter(
        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        data_source_id=proposal.data_source_id,
    )
    return [project for project in siblings if data_paths_overlap(project.data_path, proposal.data_path)]


def _not_manual_finding(proposal, project):
    """Return the finding for a Project staging would reuse under a policy that would activate it."""
    label = dict(ActivationPolicyChoices)[project.activation_policy]
    return {
        'level': BLOCKING,
        'code': 'project_not_manual',
        'pk': None,
        'path': proposal.data_path,
        'message': _(
            'Script Project "{name}" already holds what project "{key}" would '
            'stage, and its activation policy is {label}. Staging would declare the built-in modules on '
            'it and validation would then put them into service. Set it to Manual, then run this again.'
        ).format(name=project.name, key=proposal.key, label=label),
    }


def _conflict_finding(proposal, project):
    """Return the finding for a Project whose data path the proposed one could not sit beside."""
    return {
        'level': BLOCKING,
        'code': 'project_conflict',
        'pk': None,
        'path': proposal.data_path,
        'message': _(
            'Script Project "{name}" holds {existing_path}, which overlaps '
            'the {proposed_path} this migration proposes. One data source cannot carry '
            'two projects whose paths contain one another. Move or remove one of them, then run this again.'
        ).format(
            name=project.name,
            existing_path=project.data_path or _ROOT_NAME,
            proposed_path=proposal.data_path or _ROOT_NAME,
        ),
    }


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
    _source_type, data_source_id, folder = identity
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
        message = _('{path} could not be read: {error}').format(path=path, error=error)
        return dialects.UNPARSABLE, [_finding(BLOCKING, 'source_unreadable', module, message)]

    dialect = dialects.classify(body)
    findings = [
        *_path_findings(module, proposal, path),
        *_source_findings(module, body, dialect),
        *_dialect_findings(module, dialect, path),
    ]
    if any(finding['level'] == BLOCKING for finding in findings):
        # Its message says the module migrates as a helper file, which is untrue for one staging
        # refuses outright, and the finding that refuses it is already in the report.
        return dialect, findings
    if not dialects.publishes(module.scripts, body):
        # A helper and a module that stopped importing look identical from the built-in rows, so
        # the operator is told rather than either one being guessed at.
        message = _(
            '{path} publishes no Script and defines no class that could, so it migrates as a helper '
            'file rather than a script file. If it should publish one, check that its built-in '
            'module still imports.'
        ).format(path=path)
        findings.append(_finding(WARNING, 'publishes_nothing', module, message))
    return dialect, findings


def _path_findings(module, proposal, path):
    """Return the findings for where one module's content would be staged."""
    try:
        staged = proposal.source_path_for(module)
    except ValidationError as error:
        message = _('{path} cannot be staged: {error}').format(path=path, error=error.messages[0])
        return [_finding(BLOCKING, 'not_importable', module, message)]
    if staged is None:
        message = _('{path} sits outside the {name} project directory, so it cannot be staged.').format(
            path=path, name=proposal.name
        )
        return [_finding(BLOCKING, 'not_importable', module, message)]
    try:
        source_path_to_dotted_name(staged)
    except ValidationError as error:
        message = _('{path} cannot be imported as {staged}: {error}').format(
            path=path, staged=staged, error=error.messages[0]
        )
        return [_finding(BLOCKING, 'not_importable', module, message)]
    return []


def _dialect_findings(module, dialect, path):
    """Return the finding one module's authoring dialect produces, if it produces one."""
    if dialect == dialects.REPORT_STYLE:
        message = _('{path} is report-style. {hint}').format(path=path, hint=MIGRATION_HINTS['extras.reports'])
        return [_finding(BLOCKING, 'report_style', module, message)]
    if dialect == dialects.LEGACY_IMPORT:
        message = _('{path} imports the legacy authoring API. {hint}').format(
            path=path, hint=MIGRATION_HINTS['extras.scripts']
        )
        return [_finding(WARNING, 'legacy_import', module, message)]
    if dialect == dialects.UNPARSABLE:
        message = _('{path} is not valid Python.').format(path=path)
        return [_finding(BLOCKING, 'unparsable', module, message)]
    return []


def _source_findings(module, body, dialect):
    """Return the findings one module's own source produces, for imports and for re-exports."""
    # classify() returns UNPARSABLE for exactly the input ast.parse refuses, so this parse cannot
    # raise once that branch is taken.
    if dialect == dialects.UNPARSABLE:
        return []
    tree = ast.parse(body)
    path = _path(module)
    findings = []
    for name, conditional in sorted(_unresolvable_imports(tree).items()):
        if conditional:
            # Refusing the whole pass over a branch that may never run costs more than the accurate
            # refusal validation records against the one revision if it does.
            message = _(
                '{path} imports {name} only inside a conditional branch, and it is neither a '
                'standard-library module nor a distribution installed here. Reading the source '
                'cannot say whether that branch runs, so this does not refuse the migration. If it '
                'does run, validation records the failure against the revision rather than here.'
            ).format(path=path, name=name)
            findings.append(_finding(WARNING, 'import_unresolvable_in_branch', module, message))
            continue
        message = _(
            '{path} imports {name}, which is neither a standard-library module nor a distribution '
            'installed here, so the module cannot import and no verdict can ever be reached for it. '
            'A plain import never reaches a file beside it, so make it relative if that is the intent.'
        ).format(path=path, name=name)
        findings.append(_finding(BLOCKING, 'import_unresolvable', module, message))
    defined = {node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}
    for script in module.scripts:
        # A soft-deleted class left the file on purpose and has no counterpart to lose.
        if not script.is_executable or script.name in defined:
            continue
        message = _(
            '{path} does not define {name}, which the built-in feature publishes from it. A '
            'class defined elsewhere in the revision publishes only through a script_order listing, '
            'and it publishes under the module that defines it, so {path}.{name} is not an '
            'identity a migration can preserve. Move the class into this file to keep it.'
        ).format(path=path, name=script.name)
        findings.append(_finding(WARNING, 'script_not_defined_here', module, message))
    return findings


def _unresolvable_imports(tree):
    """Return the unguarded top-level names nothing here provides, mapped to whether each is conditional."""
    guarded = _guarded_imports(tree)
    names = {}
    # A name imported inside a function is resolved when that function runs, so its absence is a
    # runtime failure for one script rather than a module that cannot load at all.
    for node, conditional in dialects.reachable_nodes(tree, skip_functions=True):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            found = {alias.name.split('.')[0] for alias in node.names}
        # A relative import resolves inside the revision package by construction.
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found = {node.module.split('.')[0]}
        else:
            continue
        for name in found:
            # One unconditional import settles the name, however many guarded spellings follow it.
            names[name] = names.get(name, True) and conditional
    return {name: value for name, value in names.items() if not _resolves(name)}


def _guarded_imports(tree):
    """Return the ids of import nodes whose module already handles them being absent."""
    guarded = set()
    for block in ast.walk(tree):
        if not isinstance(block, ast.Try) or not any(_handles_missing(h) for h in block.handlers):
            continue
        # The body only: a name imported in the handler is what runs when the first one failed,
        # and nothing catches that one.
        for statement in block.body:
            for node in ast.walk(statement):
                if isinstance(node, ast.Import | ast.ImportFrom):
                    guarded.add(id(node))
    return guarded


def _handles_missing(handler):
    """Whether one except clause catches a module that is not there."""
    names = [handler.type] if not isinstance(handler.type, ast.Tuple) else list(handler.type.elts)
    # A bare except catches everything, so it covers this too.
    return handler.type is None or any(
        isinstance(name, ast.Name) and name.id in ('ImportError', 'ModuleNotFoundError', 'Exception') for name in names
    )


def _resolves(name):
    """Whether one top-level module name can be found on this host."""
    if name in sys.stdlib_module_names:
        return True
    try:
        # The top-level name only: find_spec('a.b') imports 'a', and an inventory must never
        # execute an operator's dependencies to describe them.
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _finding(level, code, module, message):
    """Return one finding, carrying the module it belongs to."""
    return {'level': level, 'code': code, 'pk': module.pk, 'path': _path(module), 'message': message}


def existing_project(project_plan):
    """Return the existing Project identified by a staging proposal, or None."""
    if project_plan.source_type == ProjectSourceTypeChoices.UPLOAD:
        return ScriptProject.objects.filter(key=project_plan.key).first()
    return ScriptProject.objects.filter(
        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        data_source_id=project_plan.data_source_id,
        data_path=project_plan.data_path,
    ).first()
