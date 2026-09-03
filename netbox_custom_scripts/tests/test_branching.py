import contextlib
import sys
import types
import unittest
from contextvars import ContextVar
from unittest import mock

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.test import TestCase, override_settings

from netbox_custom_scripts import branching
from netbox_custom_scripts.execution import CHANGELOGGED_PROBE_MODEL
from netbox_custom_scripts.models import (
    CustomScript,
    CustomScriptModule,
    CustomScriptProject,
    CustomScriptProjectRevision,
    MigrationRun,
)

PACKAGE = 'netbox_branching'
MODULE = f'{PACKAGE}.utilities'
CONTEXTVARS = f'{PACKAGE}.contextvars'

# Every model in branching.GLOBAL_MODELS, so a model added there without being listed here fails
# rather than going unchecked.
GLOBAL_MODELS = (
    CustomScript,
    CustomScriptModule,
    CustomScriptProject,
    CustomScriptProjectRevision,
    MigrationRun,
)
GLOBAL_LABELS = [
    'netbox_custom_scripts.customscript',
    'netbox_custom_scripts.customscriptmodule',
    'netbox_custom_scripts.customscriptproject',
    'netbox_custom_scripts.customscriptprojectrevision',
    'netbox_custom_scripts.migrationrun',
]

try:  # The real package, when a developer has it installed alongside this plugin.
    from netbox_branching import utilities as real_branching
except ImportError:
    real_branching = None


def fake_branching(**members):
    """
    Return sys.modules entries standing in for NetBox Branching, carrying the given callables.

    The parent package is faked alongside the submodule, so these tests behave the same whether
    or not NetBox Branching is installed. CI installs only this plugin's own dependencies, so it
    is not.
    """
    package = types.ModuleType(PACKAGE)
    module = types.ModuleType(MODULE)
    for name, value in members.items():
        setattr(module, name, value)
    package.utilities = module
    return {PACKAGE: package, MODULE: module}


def fake_contextvars(branch=None):
    """Return a sys.modules entry standing in for the branching contextvar, holding one branch."""
    module = types.ModuleType(CONTEXTVARS)
    module.active_branch = ContextVar('active_branch', default=None)
    module.active_branch.set(branch)
    return {CONTEXTVARS: module}


@contextlib.contextmanager
def branching_installed(module):
    """Make NetBox Branching look installed, with the given stand-in for its utilities module."""
    with mock.patch.object(branching.apps, 'is_installed', return_value=True), mock.patch.dict(sys.modules, module):
        yield


def routing(**results):
    """
    Return a branching install whose routing answers per model name.

    A name absent from results is routed to the main schema, which is the safe answer, so a test
    names only the models it wants treated as branch-aware.
    """
    return branching_installed(
        fake_branching(supports_branching=lambda model: results.get(model._meta.model_name, False))
    )


def routing_raising(error):
    """Return a branching install whose routing function raises."""

    def explode(model):
        raise error

    return branching_installed(fake_branching(supports_branching=explode))


def routing_api_missing():
    """Return a branching install whose utilities module cannot be imported."""
    return branching_installed({MODULE: None})


def fake_model(model_name, app_label=branching.APP_LABEL):
    """Return the smallest stand-in the resolver accepts, for a model that does not exist yet."""
    return types.SimpleNamespace(_meta=types.SimpleNamespace(app_label=app_label, model_name=model_name))


class ResolverTestCase(TestCase):
    def test_this_suite_covers_every_global_model(self):
        # Both fixtures above are hand-written, so a model added to branching.GLOBAL_MODELS without
        # being added here would be routed to the main schema and never checked, and would be left
        # out of the hint an operator acts on.
        self.assertEqual(
            sorted(model._meta.model_name for model in GLOBAL_MODELS),
            sorted(branching.GLOBAL_MODELS),
        )
        self.assertEqual(
            GLOBAL_LABELS,
            sorted(f'{branching.APP_LABEL}.{name}' for name in branching.GLOBAL_MODELS),
        )

    def test_the_global_models_are_not_branchable(self):
        # False is what routes a model to the main schema, so one project keeps one row set,
        # one source tree, and one entrypoint configuration no matter which branch is active.
        for model in GLOBAL_MODELS:
            with self.subTest(model=model.__name__):
                self.assertIs(branching.resolve_branching_support(model), False)

    def test_a_model_outside_global_models_is_left_alone(self):
        # None defers, so a model added to this plugin later keeps NetBox's ordinary behaviour,
        # which is the right default for one that holds no storage or validation configuration.
        # The name has to stay fictional, because every model this plugin ships is listed.
        self.assertIsNone(branching.resolve_branching_support(fake_model('unlistedmodel')))

    def test_other_applications_are_left_alone(self):
        from core.models import DataSource

        self.assertIsNone(branching.resolve_branching_support(DataSource))

    def test_register_hands_the_resolver_to_branching(self):
        # The one thing register() does, asserted here because the real-package test below cannot:
        # by the time it runs, AppConfig.ready() has already registered this resolver.
        registered = []
        with branching_installed(fake_branching(register_branching_resolver=registered.append)):
            branching.register()
        self.assertEqual(registered, [branching.resolve_branching_support])

    def test_register_is_a_no_op_without_the_resolver_api(self):
        # Registration is best effort, so an absent or changed API is not an error by itself.
        # unsafe_routing_reason() is what decides whether the result is safe.
        with routing_api_missing():
            branching.register()


class RoutingReasonTestCase(TestCase):
    def test_no_reason_under_either_test_configuration(self):
        # Asserted rather than assumed, and it holds for either configuration this suite runs
        # under: branching is absent from the default one, and routes every global model to main
        # under the branching one. The name therefore names the outcome, not the cause.
        self.assertIsNone(branching.unsafe_routing_reason())

    def test_no_reason_when_every_global_model_stays_global(self):
        with routing():
            self.assertIsNone(branching.unsafe_routing_reason())

    def test_a_reason_naming_only_the_model_routed_to_a_branch(self):
        with routing(customscriptproject=True):
            reason = branching.unsafe_routing_reason()
        self.assertIn('netbox_custom_scripts.customscriptproject', reason)
        self.assertNotIn('customscriptprojectrevision', reason)
        self.assertNotIn('customscriptmodule', reason)

    def test_a_reason_when_the_routing_api_is_unavailable(self):
        with routing_api_missing():
            reason = branching.unsafe_routing_reason()
        self.assertIn('supports_branching API is unavailable', reason)

    def test_a_reason_when_the_routing_api_raises(self):
        # Fail closed. An answer that cannot be obtained is not an answer that these models are
        # safe, and this test exists so nobody turns that into a pass.
        with routing_raising(RuntimeError('branching is misconfigured')):
            reason = branching.unsafe_routing_reason()
        self.assertIn('could not report', reason)
        self.assertIn('branching is misconfigured', reason)


class ProbeReasonTestCase(TestCase):
    """Whether the model a run probes can still report where a change-logged write goes."""

    def probe(self):
        return apps.get_model(*CHANGELOGGED_PROBE_MODEL)

    def test_no_reason_under_either_test_configuration(self):
        # Branching is absent from the default configuration and routes dcim.device to a branch
        # under the branching one, so the answer is the same either way.
        self.assertIsNone(branching.probe_unusable_reason(self.probe()))

    def test_no_reason_while_the_probe_model_is_branch_aware(self):
        with routing(device=True):
            self.assertIsNone(branching.probe_unusable_reason(self.probe()))

    def test_a_reason_naming_the_model_once_it_is_no_longer_branch_aware(self):
        with routing():
            reason = branching.probe_unusable_reason(self.probe())

        self.assertIn('dcim.device', reason)
        self.assertIn('no longer routes', reason)

    def test_a_reason_when_the_routing_api_is_unavailable(self):
        with routing_api_missing():
            self.assertIn('supports_branching API is unavailable', branching.probe_unusable_reason(self.probe()))

    def test_a_reason_when_the_routing_api_raises(self):
        with routing_raising(RuntimeError('branching is misconfigured')):
            reason = branching.probe_unusable_reason(self.probe())

        self.assertIn('could not report', reason)
        self.assertIn('branching is misconfigured', reason)


class ActiveBranchTestCase(TestCase):
    """Reading the branch a context has selected, which is what the routing decision reads."""

    def test_no_branch_when_branching_is_absent(self):
        self.assertIsNone(branching.active_branch_name())

    def test_the_branch_the_contextvar_holds(self):
        with branching_installed(fake_contextvars('fixing-hq')):
            self.assertEqual(branching.active_branch_name(), 'fixing-hq')

    def test_no_branch_when_the_contextvar_is_unset(self):
        with branching_installed(fake_contextvars()):
            self.assertIsNone(branching.active_branch_name())

    def test_no_branch_when_the_module_is_unavailable(self):
        with branching_installed({CONTEXTVARS: None}):
            self.assertIsNone(branching.active_branch_name())


class RoutingCheckTestCase(TestCase):
    def test_no_error_when_routing_is_safe(self):
        self.assertEqual(branching.check_routing(None), [])

    def test_one_error_carrying_the_configuration_when_routing_is_unsafe(self):
        with routing(customscriptproject=True):
            errors = branching.check_routing(None)
        self.assertEqual([error.id for error in errors], ['netbox_custom_scripts.E001'])
        self.assertIn('storage operations are refused', errors[0].msg)
        for label in GLOBAL_LABELS:
            self.assertIn(label, errors[0].hint)

    def test_the_hint_is_actionable_when_the_routing_api_is_unavailable(self):
        # Adding the exemption cannot clear this case, because the routing still cannot be
        # confirmed, so the hint has to name the release requirement too or it sends an operator
        # to do something they may already have done.
        with routing_api_missing():
            errors = branching.check_routing(None)
        self.assertIn('supports_branching API is unavailable', errors[0].msg)
        self.assertIn('release that exposes the supports_branching API', errors[0].hint)


class RequireSafeRoutingTestCase(TestCase):
    def test_permitted_when_routing_is_safe(self):
        self.assertIsNone(branching.require_safe_routing())

    def test_refused_when_routing_is_unsafe(self):
        with routing(customscriptprojectrevision=True), self.assertRaises(ImproperlyConfigured) as raised:
            branching.require_safe_routing()
        self.assertIn('exempt_models', str(raised.exception))


@unittest.skipIf(real_branching is None, 'netbox_branching is not installed')
# Not exercising the exemption: supports_branching() reads the branching plugin's own
# configuration through get_plugin_config, which raises when PLUGINS_CONFIG has no entry for it,
# and this project's test settings do not enable it. An empty entry is all that lookup needs.
@override_settings(PLUGINS_CONFIG={'netbox_branching': {}})
class RealBranchingApiTestCase(TestCase):
    """
    Verify the effective routing against the installed NetBox Branching package.

    The routing decision needs neither a database nor a provisioned branch, so it can be checked
    against the real supports_branching() rather than a stand-in, using only the function NetBox
    Branching exports.

    What this deliberately does not do is establish which mechanism produced the answer.
    AppConfig.ready() has already registered this plugin's resolver by the time any test runs, and
    a resolver answers before the exempt list is consulted, so these assertions would pass with or
    without an exemption configured. Isolating the exemption would mean clearing NetBox Branching's
    private resolver list, which is coupling this suite deliberately does not have. The resolver
    handover is asserted directly in ResolverTestCase instead.

    Provisioning a branch is what covers reads and writes inside one, tag and journal behaviour
    there, deletion, and merge. That lives in tests/test_branching_provisioned.py, which runs
    under testing/configuration_branching.py against a database of its own.
    """

    def test_the_effective_routing_keeps_the_global_models_in_main(self):
        for model in GLOBAL_MODELS:
            with self.subTest(model=model.__name__):
                self.assertFalse(real_branching.supports_branching(model))

    def test_tags_and_journal_entries_stay_branch_aware(self):
        # The documented split: a project is installation-global while its tag assignments and
        # journal entries follow branching's ordinary behaviour. If any of these ever became
        # global, the documentation would be wrong.
        from django.apps import apps as live_apps

        for label in ('extras.Tag', 'extras.TaggedItem', 'extras.JournalEntry'):
            with self.subTest(label=label):
                self.assertTrue(real_branching.supports_branching(live_apps.get_model(label)))
