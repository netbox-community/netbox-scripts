"""
Keep this plugin's models out of NetBox Branching's per-branch schemas.

A Script Project and its revisions address one source tree through the project's storage
key and the revision's digest. That path carries no branch or schema component, so rows living in
two schemas would name the same bytes on disk, and a revision deleted inside a branch would
remove source that the main schema still serves. Script File rows are part of that same configuration:
script file declarations feed the snapshot a revision is validated against, so a branch-local set
of declarations would change what an installation-global revision means.

NetBox Branching's own exempt_models setting is the documented way to route models to the main
schema, and this module registers a resolver so that a fresh installation is already right before
an operator configures anything. Either way the decision belongs to NetBox Branching: routing
questions here go to the public supports_branching(), and which branch a context selected is read
from the contextvar the routing decision itself reads, so nothing reimplements the policy behind
either answer.

NetBox Branching is optional. When it is absent there is one schema and these models are global
already, so nothing here does anything.
"""

import logging
from contextlib import contextmanager, nullcontext

from django.apps import apps
from django.core.checks import Error
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _

APP_LABEL = 'netbox_scripts'
BRANCHING_APP_LABEL = 'netbox_branching'

# The models this plugin routes to the main schema instead of letting a branch copy them.
#
# A model added to this plugin later is not covered and keeps NetBox's ordinary branching
# behaviour, which is the right default for one that holds no storage. Two things are worth
# checking when adding one: whether it owns bytes on disk, in which case it belongs here, and
# whether it gains a concrete relation to a branch-aware model, which would leave a row in the
# main schema pointing at a row that exists only inside a branch. Scripts hold no bytes
# and are listed for a third reason: activation writes them in the same transaction that moves
# a project's active revision, so splitting them across schemas would split that transaction.
# A migration run is listed for a fourth reason: a run started inside a branch would leave the
# cutover state invisible on the main schema, so the built-in feature would look un-fenced to
# everything outside that branch.
GLOBAL_MODELS = (
    'migrationrun',
    'netboxscript',
    'scriptfile',
    'scriptproject',
    'scriptprojectrevision',
)

# The two things unsafe routing can need, in one hint because there is one error path. Which one
# applies is already named by the reason the check and the guard report, and the configuration on
# its own cannot resolve a missing inspection API, because the routing then cannot be confirmed
# either way. The labels are individual rather than a netbox_scripts.* wildcard, so a model
# added later is not swept in with them.
ROUTING_HINT = _(
    'Use a NetBox Branching release that exposes the supports_branching API, since this plugin '
    'cannot confirm the routing without it. If these models are still routed to a branch, add the '
    "labels to PLUGINS_CONFIG['netbox_branching']['exempt_models']: "
    '"netbox_scripts.netboxscript", '
    '"netbox_scripts.scriptfile", '
    '"netbox_scripts.scriptproject", '
    '"netbox_scripts.scriptprojectrevision", '
    '"netbox_scripts.migrationrun".'
)

logger = logging.getLogger('netbox.plugins.netbox_scripts.branching')


def resolve_branching_support(model):
    """
    Return False for this plugin's global models and None for everything else.

    False tells NetBox Branching to route a model to the main schema. None defers to the next
    resolver and then to the default change-logging heuristic, which is what every other model
    keeps, including any model of this plugin's not named in GLOBAL_MODELS.
    """
    if model._meta.app_label == APP_LABEL and model._meta.model_name in GLOBAL_MODELS:
        return False
    return None


def register():
    """
    Offer the resolver to NetBox Branching, on a best-effort basis.

    Nothing here decides whether the models are safe, so a resolver API that is absent or has
    changed shape is not a failure by itself: an operator may have configured exempt_models
    instead, which reaches the same routing. unsafe_routing_reason() is what confirms the result.
    """
    try:
        from netbox_branching.utilities import register_branching_resolver

        register_branching_resolver(resolve_branching_support)
    except Exception as error:
        logger.debug('Could not register a branching resolver with NetBox Branching: %s', error)


def unsafe_routing_reason():
    """
    Return why NetBox Branching would not keep these models in the main schema, or None.

    One helper with two callers: the system check reports what it returns, and the storage
    operations refuse on it. Only the effective answer matters here, so this asks
    supports_branching() rather than auditing how branching arrived at it, which leaves that
    policy where it belongs.
    """
    if not apps.is_installed(BRANCHING_APP_LABEL):
        return None

    try:
        from netbox_branching.utilities import supports_branching
    except ImportError:
        return _('NetBox Branching is installed, but its supports_branching API is unavailable.')

    try:
        branch_aware = [
            f'{APP_LABEL}.{name}' for name in GLOBAL_MODELS if supports_branching(apps.get_model(APP_LABEL, name))
        ]
    except Exception as error:
        # Fail closed on purpose. An answer that cannot be obtained is not an answer that these
        # models are safe, so this must not become a pass.
        return _("NetBox Branching could not report how this plugin's models are routed: {error}").format(error=error)

    if branch_aware:
        return _('NetBox Branching routes {models} to a branch schema.').format(models=', '.join(branch_aware))
    return None


def probe_unusable_reason(model):
    """Return why a model can no longer report the database a change-logged write takes, or None."""
    if not apps.is_installed(BRANCHING_APP_LABEL):
        return None
    label = f'{model._meta.app_label}.{model._meta.model_name}'
    try:
        from netbox_branching.utilities import supports_branching
    except ImportError:
        return _(
            'NetBox Branching is installed, but its supports_branching API is unavailable, so {label} '
            'cannot report where a change-logged write goes.'
        ).format(label=label)
    try:
        if supports_branching(model):
            return None
    except Exception as error:
        return _('NetBox Branching could not report how {label} is routed: {error}.').format(
            label=label, error=str(error).rstrip('.')
        )
    return _(
        'NetBox Branching no longer routes {label} to a branch schema, so it cannot report where a '
        'change-logged write goes.'
    ).format(label=label)


def active_branch_name():
    """Return the name of the branch this context has selected, or None when there is not one."""
    if not apps.is_installed(BRANCHING_APP_LABEL):
        return None
    try:
        # The same contextvar the routing decision reads, so this cannot disagree with it.
        from netbox_branching.contextvars import active_branch
    except ImportError:
        logger.debug('NetBox Branching is installed but exposes no active_branch, reading no branch as set.')
        return None
    branch = active_branch.get()
    return str(branch) if branch else None


def require_safe_routing():
    """
    Refuse an operation that would write or remove source NetBox Branching may not keep in main.

    Raises ImproperlyConfigured, which is what the condition is: a misconfiguration rather than a
    storage fault, and deliberately not a StorageError, so no caller records it as a failed
    revision.
    """
    if reason := unsafe_routing_reason():
        raise ImproperlyConfigured(f'{reason} {ROUTING_HINT}')


@contextmanager
def main_schema_only():
    """
    Hold the enclosed block on the main schema, whatever branch a request selected.

    Does nothing when NetBox Branching is absent, which is one schema anyway, and nothing when it
    exposes no deactivation API, where a branch a request selected does stay active.
    """
    with _deactivation():
        yield


def _deactivation():
    """Return NetBox Branching's deactivation context, or a no-op stand-in."""
    if not apps.is_installed(BRANCHING_APP_LABEL):
        return nullcontext()
    try:
        from netbox_branching.utilities import deactivate_branch
    except ImportError:
        logger.debug('NetBox Branching is installed but exposes no deactivate_branch, leaving the branch as set.')
        return nullcontext()
    return deactivate_branch()


def check_routing(app_configs, **kwargs):
    """
    Report unsafe routing, as the second line of defence behind the storage guard.

    A system check does not run under a WSGI server, so this reports the problem where an
    administrator will see it while the guard is what actually protects the source tree.
    """
    if reason := unsafe_routing_reason():
        return [
            Error(
                _('{reason} Script Project storage operations are refused until it is resolved.').format(reason=reason),
                hint=ROUTING_HINT,
                id='netbox_scripts.E001',
            )
        ]
    return []
