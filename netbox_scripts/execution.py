"""
The context one Custom Script run happens inside.

A run is not a plain function call. Database changes have to land in the right database and be
reverted whole on a dry run or an error, change attribution and event delivery have to behave
as they do for a request, and anything the script raises has to reach the run log before it
reaches the Job. This module owns all of that, so the Job layer decides only what to run and
what to record.

NetBox exposes none of it to plugins as a documented API. Six symbols here are internal:
registry['request_processors'], event_tracking, current_request, clear_events, the router
probe that finds the database change-logged writes go to, and the abort a script carried over
from the built-in feature raises. They are confined to this module on purpose, so the generic
execution context requested from NetBox replaces one file rather than a scattering, and the
last of them belongs to the feature NetBox retires at v5.0.
"""

import logging
import traceback
from contextlib import ExitStack

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS, router, transaction
from django.utils.translation import gettext as _

from core.signals import clear_events
from netbox.context import current_request
from netbox.context_managers import event_tracking
from netbox.registry import registry
from utilities.exceptions import AbortScript as LegacyAbortScript

from . import branching
from .runtime.exceptions import (
    DiscoveryError,
    InvalidModulePathError,
    ScriptFileImportError,
    ScriptMetadataError,
    ScriptResolutionError,
)
from .runtime.loader import revision_import_session, unload_revision
from .runtime.resolution import resolve_script_class
from .scripts.exceptions import AbortScript
from .storage import config
from .storage.exceptions import RevisionCorruptError, StorageError
from .validation import build_error_sanitizer

__all__ = (
    'LOAD_FAILURES',
    'RESOLUTION_FAILURES',
    'ScriptNotExecutableError',
    'load_script_class',
    'run_script',
)

# Everything that means "this revision cannot give us the class the row names". Each one is a
# statement about content or configuration, so a run fails rather than being retried blindly.
RESOLUTION_FAILURES = (
    DiscoveryError,
    InvalidModulePathError,
    RevisionCorruptError,
    ScriptFileImportError,
    ScriptMetadataError,
    ScriptResolutionError,
)

# load_script_class also reaches the store and the local cache.
LOAD_FAILURES = RESOLUTION_FAILURES + (StorageError, OSError)


class ScriptNotExecutableError(Exception):
    """Raised when a Custom Script is asked to run while something is holding it back."""


# Both classes end a run cleanly. The plugin's own is what its authoring API documents, and the
# other reaches us from scripts carried over unchanged, which raise what they were written
# against. Neither is a subclass of the other, so membership has to be spelled out.
ABORT_EXCEPTIONS = (AbortScript, LegacyAbortScript)

# A script writes to core objects, and which database those writes land in is a question only
# the router can answer, per model. This plugin's own models are deliberately routed to the main
# schema (see branching.py), so probing one of them would report the main alias even while a
# branch is active. The probe is therefore an ordinary branch-aware core model, chosen for
# nothing but being one. It goes away once NetBox exposes an execution context that routes.
CHANGELOGGED_PROBE_MODEL = ('dcim', 'Device')

logger = logging.getLogger('netbox.plugins.netbox_scripts.execution')


class _DryRunRollback(Exception):
    """Raised at the end of a dry run to roll its transaction back."""


def run_script(instance, *, data, commit, request=None):
    """
    Run one Script instance under the transaction, request, and event context a run needs.

    Sets output on the instance and appends to its run log, including a final line saying
    whether changes were kept. Re-raises whatever the script raised, after logging it, so the
    caller decides what a failed run does to its Job. A dry run reverts every change and is
    not a failure. request may be None, for a run with no request behind it.
    """
    # Entering event tracking makes this run's request the current one, and leaving it puts
    # back None rather than what was there before, and only on the way out of a clean exit. A
    # run can fail, and can be nested inside a request, so the previous value is restored here.
    # Without this a worker keeps attributing later changes to an abandoned run's user.
    outer_request = current_request.get()
    try:
        with ExitStack() as stack:
            for processor in registry['request_processors']:
                # Event tracking queues events to flush at the end. A dry run reverts
                # everything, so entering it would only build a queue to discard.
                if not commit and processor is event_tracking:
                    continue
                try:
                    stack.enter_context(processor(request))
                except Exception as error:
                    # The registry is shared with every other installed plugin, so one
                    # processor that cannot start is not this run's fault and must not end it.
                    # Event tracking is the exception: a committed run that silently emits
                    # nothing is worse than one that fails, so its failure is the run's.
                    if processor is event_tracking:
                        raise
                    logger.warning(
                        'Running without the request processor %s, it could not start: %s',
                        getattr(processor, '__name__', processor),
                        error,
                    )
            # Innermost, and after the processors on purpose: one of them has just reactivated
            # whatever branch the requesting user had, off the request the Job carries a copy of.
            stack.enter_context(branching.main_schema_only())
            _execute(instance, data=data, commit=commit, request=request)
    finally:
        current_request.set(outer_request)


def load_script_class(script):
    """
    Return the class one Custom Script row names, out of the revision its project serves.

    This is the same resolution the worker performs, against the same revision, so a form built
    from the class matches the source that will execute. The namespace is unloaded before this
    returns, so the class comes back good for introspection rather than for a run. Raises
    ScriptResolutionError when the project serves no revision, when the active revision does not
    publish the row's identity, and for any load failure, whose message is sanitized.
    """
    identity = f'{script.module_path}.{script.class_name}'
    revision = script.project.active_revision
    if revision is None:
        # A project can be deactivated between a caller's is_executable check and this call.
        raise ScriptResolutionError(
            f'This project is not serving a revision, so "{identity}" cannot be loaded.',
            code='not_serving',
            name=identity,
        )
    storage_key = str(script.project.storage_key)
    sanitize = build_error_sanitizer(storage_key, revision.digest)
    with revision_import_session(storage_key, revision.digest):
        try:
            return resolve_script_class(
                storage_key,
                revision.digest,
                discovered_scripts=revision.discovered_scripts,
                project_key=script.project.key,
                module_path=script.module_path,
                class_name=script.class_name,
                storage=config.get_storage(),
                manifest=revision.manifest,
            )
        except LOAD_FAILURES as error:
            # Cache errors carry paths built from the storage key and digest, and this is the
            # only caller holding both. Collapsed to one class: no call site branches on the type.
            raise ScriptResolutionError(sanitize(str(error)), code='load_failed', name=identity) from error
        finally:
            unload_revision(storage_key, revision.digest)


def _execute(instance, *, data, commit, request):
    """Call the script inside its transactions, converting whatever escapes into log records."""
    try:
        try:
            probe = apps.get_model(*CHANGELOGGED_PROBE_MODEL)
            if reason := branching.probe_unusable_reason(probe):
                # A branch is still active here only when main_schema_only() found no
                # deactivation API. The router would then route a write to it while the probe
                # reads the default alias, so nothing covers that write.
                if branch := branching.active_branch_name():
                    raise ImproperlyConfigured(
                        f'{reason} Branch {branch} is active, so changes this run makes could not be '
                        f'rolled back with it.'
                    )
                instance.log_warning(reason)
            changelogged_alias = router.db_for_write(probe)
            with transaction.atomic(using=DEFAULT_DB_ALIAS):
                # Nothing may sit between these two blocks. A statement here runs after the
                # inner block has already unwound, so its writes would commit to the default
                # database while the routed ones rolled back.
                if changelogged_alias != DEFAULT_DB_ALIAS:
                    with transaction.atomic(using=changelogged_alias):
                        _call(instance, data, commit)
                else:
                    _call(instance, data, commit)
        except _DryRunRollback:
            instance.log_info(_('Database changes have been reverted, this was a dry run.'))
    except Exception as error:
        _log_failure(instance, error)
        instance.log_info(_('Database changes have been reverted because the run did not finish.'))
        # The queue is request-scoped, and this run is abandoning changes the events describe.
        if request is not None:
            clear_events.send(request)
        raise


def _call(instance, data, commit):
    """Run the script once, ending a dry run with the exception that reverts it."""
    instance.output = instance.run(data, commit)
    if not commit:
        raise _DryRunRollback()


def _log_failure(instance, error):
    """Record one unfinished run on the script's own log, with detail matched to the cause."""
    if isinstance(error, ABORT_EXCEPTIONS):
        # A deliberate stop. The author already said why, so a traceback would only bury it.
        instance.log_failure(_('The script stopped with an error: {error}').format(error=error))
        return
    instance.log_failure(
        _('An exception occurred: `{name}: {error}`').format(name=type(error).__name__, error=error)
        + f'\n```\n{traceback.format_exc()}\n```'
    )
