import sys
import tempfile
import threading
import uuid
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage
from django.test import TestCase

from netbox_scripts.runtime import loader
from netbox_scripts.runtime.exceptions import EntrypointImportError, InvalidModulePathError
from netbox_scripts.runtime.naming import PRIVATE_ROOT, project_module_name, revision_module_name
from netbox_scripts.storage.exceptions import RevisionCorruptError
from netbox_scripts.storage.manifest import compute_digest
from netbox_scripts.storage.paths import revision_key
from netbox_scripts.tests.runtime.test_cache import discard_tree
from netbox_scripts.tests.storage.test_store import STORAGE_KEY, OpenRecordingStorage, manifest_for

OTHER_KEY = uuid.UUID('5b7e2f10-9c4d-4a6b-b1e8-7d3f5a2c9e41')

# Revision fixtures import this module back and mutate these, so a test can observe what
# project code actually did.
ROOT_RUNS = []

ROOT_INIT = b'from netbox_scripts.tests.runtime.test_loader import ROOT_RUNS\nROOT_RUNS.append("run")\n'


class FakeTimeout(BaseException):
    """Stands in for a worker timeout, the exception shape passthrough exists for."""


def seed_revision(storage, storage_key, files):
    """Store one file mapping as revision content, returning its manifest and digest."""
    manifest = manifest_for(files)
    digest = compute_digest(manifest)
    for path, content in files.items():
        storage.save(revision_key(storage_key, digest, path), ContentFile(content))
    return manifest, digest


class LoaderTestCase(TestCase):
    def setUp(self):
        self.storage = InMemoryStorage()
        self.cache_root = Path(tempfile.mkdtemp())
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        ROOT_RUNS.clear()

    def _purge_namespace(self):
        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def load(self, storage_key, files, entrypoint, **kwargs):
        manifest, digest = seed_revision(self.storage, storage_key, files)
        module = self.load_again(storage_key, digest, entrypoint, manifest, **kwargs)
        return module, manifest, digest

    def load_again(self, storage_key, digest, entrypoint, manifest, **kwargs):
        return loader.import_entrypoint(
            storage_key,
            digest,
            entrypoint,
            storage=kwargs.pop('storage', self.storage),
            manifest=manifest,
            cache_root=self.cache_root,
            **kwargs,
        )


class ImportBehaviorTestCase(LoaderTestCase):
    def test_two_projects_each_import_their_own_utils(self):
        files = {'probe.py': b'from . import utils\nVALUE = utils.VALUE\n'}
        module_a, _, _ = self.load(STORAGE_KEY, {**files, 'utils.py': b'VALUE = "alpha"\n'}, 'probe.py')
        module_b, _, _ = self.load(OTHER_KEY, {**files, 'utils.py': b'VALUE = "beta"\n'}, 'probe.py')
        self.assertEqual(module_a.VALUE, 'alpha')
        self.assertEqual(module_b.VALUE, 'beta')
        self.assertIsNot(module_a, module_b)

    def test_two_revisions_of_one_project_coexist(self):
        old, _, old_digest = self.load(STORAGE_KEY, {'probe.py': b'VALUE = 1\n'}, 'probe.py')
        new, _, new_digest = self.load(STORAGE_KEY, {'probe.py': b'VALUE = 2\n'}, 'probe.py')
        self.assertNotEqual(old_digest, new_digest)
        self.assertEqual((old.VALUE, new.VALUE), (1, 2))
        self.assertIn(revision_module_name(STORAGE_KEY, old_digest), sys.modules)
        self.assertIn(revision_module_name(STORAGE_KEY, new_digest), sys.modules)

    def test_revision_modules_never_shadow_installed_packages(self):
        import circuits as real_circuits

        files = {
            'circuits.py': b'MARKER = "shadow"\n',
            'requests.py': b'MARKER = "shadow"\n',
            'probe.py': (
                b'import circuits\n'
                b'import requests\n'
                b'from . import circuits as local_circuits\n'
                b'GLOBAL_MARKERS = (getattr(circuits, "MARKER", None), getattr(requests, "MARKER", None))\n'
                b'LOCAL_MARKER = local_circuits.MARKER\n'
            ),
        }
        module, _, _ = self.load(STORAGE_KEY, files, 'probe.py')
        self.assertEqual(module.GLOBAL_MARKERS, (None, None))
        self.assertEqual(module.LOCAL_MARKER, 'shadow')
        self.assertIs(sys.modules['circuits'], real_circuits)

    def test_relative_imports_work_without_any_init_file(self):
        files = {
            'tools/deploy.py': b'from . import helper\nVALUE = helper.VALUE\n',
            'tools/helper.py': b'VALUE = 7\n',
        }
        module, _, _ = self.load(STORAGE_KEY, files, 'tools/deploy.py')
        self.assertEqual(module.VALUE, 7)

    def test_relative_imports_reach_through_a_root_init(self):
        files = {
            '__init__.py': b'SETTING = "configured"\n',
            'deploy.py': b'from . import SETTING\nVALUE = SETTING\n',
        }
        module, _, _ = self.load(STORAGE_KEY, files, 'deploy.py')
        self.assertEqual(module.VALUE, 'configured')

    def test_a_subpackage_init_can_be_the_entrypoint(self):
        files = {'pkg/__init__.py': b'VALUE = 11\n'}
        module, _, digest = self.load(STORAGE_KEY, files, 'pkg/__init__.py')
        self.assertEqual(module.VALUE, 11)
        self.assertEqual(module.__name__, f'{revision_module_name(STORAGE_KEY, digest)}.pkg')

    def test_two_entrypoints_share_one_helper_module(self):
        files = {
            'first.py': b'from . import helpers\n',
            'second.py': b'from . import helpers\n',
            'helpers.py': b'STATE = object()\n',
        }
        first, manifest, digest = self.load(STORAGE_KEY, files, 'first.py')
        second = self.load_again(STORAGE_KEY, digest, 'second.py', manifest)
        self.assertIs(first.helpers, second.helpers)

    def test_a_root_init_executes_exactly_once(self):
        files = {'__init__.py': ROOT_INIT, 'first.py': b'VALUE = 1\n', 'second.py': b'VALUE = 2\n'}
        _, manifest, digest = self.load(STORAGE_KEY, files, 'first.py')
        self.load_again(STORAGE_KEY, digest, 'second.py', manifest)
        self.assertEqual(ROOT_RUNS, ['run'])

    def test_reimporting_an_entrypoint_returns_the_same_module(self):
        module, manifest, digest = self.load(STORAGE_KEY, {'probe.py': b'VALUE = 1\n'}, 'probe.py')
        self.assertIs(self.load_again(STORAGE_KEY, digest, 'probe.py', manifest), module)


class FailureBoundaryTestCase(LoaderTestCase):
    def assert_revision_absent(self, storage_key, digest):
        revision_name = revision_module_name(storage_key, digest)
        self.assertEqual([name for name in sys.modules if name.startswith(revision_name)], [])
        container = sys.modules.get(project_module_name(storage_key))
        if container is not None:
            self.assertFalse(hasattr(container, revision_name.rsplit('.', 1)[1]))

    def test_a_failed_entrypoint_sweeps_the_helper_it_imported(self):
        files = {
            'probe.py': b'from . import helper\nraise RuntimeError("boom")\n',
            'helper.py': b'VALUE = 1\n',
        }
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, files)
        with self.assertRaises(EntrypointImportError):
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assert_revision_absent(STORAGE_KEY, digest)

    def test_a_failing_root_init_sweeps_the_helper_it_imported(self):
        files = {
            '__init__.py': b'from . import helper\nraise RuntimeError("boom")\n',
            'helper.py': b'VALUE = 1\n',
            'probe.py': b'VALUE = 2\n',
        }
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, files)
        with self.assertRaises(EntrypointImportError):
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assert_revision_absent(STORAGE_KEY, digest)

    def test_a_failed_sibling_leaves_an_earlier_import_standing(self):
        files = {
            'good.py': b'from . import helpers\n',
            'bad.py': b'raise RuntimeError("boom")\n',
            'helpers.py': b'VALUE = 3\n',
        }
        good, manifest, digest = self.load(STORAGE_KEY, files, 'good.py')
        with self.assertRaises(EntrypointImportError):
            self.load_again(STORAGE_KEY, digest, 'bad.py', manifest)
        revision_name = revision_module_name(STORAGE_KEY, digest)
        self.assertIn(revision_name, sys.modules)
        self.assertIn(f'{revision_name}.helpers', sys.modules)
        self.assertNotIn(f'{revision_name}.bad', sys.modules)
        self.assertEqual(good.helpers.VALUE, 3)

    def test_a_sibling_revision_survives_anothers_failure(self):
        good, good_manifest, good_digest = self.load(STORAGE_KEY, {'probe.py': b'VALUE = 3\n'}, 'probe.py')
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'raise RuntimeError("boom")\n'})
        with self.assertRaises(EntrypointImportError):
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assert_revision_absent(STORAGE_KEY, digest)
        self.assertIs(self.load_again(STORAGE_KEY, good_digest, 'probe.py', good_manifest), good)

    def test_passthrough_exceptions_escape_unwrapped_after_the_sweep(self):
        files = {
            'probe.py': (b'from netbox_scripts.tests.runtime.test_loader import FakeTimeout\nraise FakeTimeout()\n'),
        }
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, files)
        with self.assertRaises(FakeTimeout):
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest, passthrough=(FakeTimeout,))
        self.assert_revision_absent(STORAGE_KEY, digest)

    def test_worker_control_exceptions_always_escape(self):
        for source in (b'raise KeyboardInterrupt()\n', b'raise GeneratorExit()\n'):
            with self.subTest(source=source):
                manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': source})
                with self.assertRaises((KeyboardInterrupt, GeneratorExit)):
                    self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
                self.assert_revision_absent(STORAGE_KEY, digest)

    def test_system_exit_wraps_as_an_import_failure(self):
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'raise SystemExit(1)\n'})
        with self.assertRaises(EntrypointImportError) as caught:
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assertIsInstance(caught.exception.__cause__, SystemExit)

    def test_a_custom_base_exception_wraps_as_an_import_failure(self):
        files = {'probe.py': b'class Hostile(BaseException):\n    pass\nraise Hostile()\n'}
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, files)
        with self.assertRaises(EntrypointImportError) as caught:
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assertEqual(type(caught.exception.__cause__).__name__, 'Hostile')

    def test_a_syntax_error_wraps_as_an_import_failure(self):
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'def broken(:\n'})
        with self.assertRaises(EntrypointImportError) as caught:
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assertIsInstance(caught.exception.__cause__, SyntaxError)
        self.assert_revision_absent(STORAGE_KEY, digest)

    def test_the_failure_detail_describes_the_original(self):
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'raise RuntimeError("boom")\n'})
        with self.assertRaises(EntrypointImportError) as caught:
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        detail = caught.exception.detail
        self.assertEqual(detail['path'], 'probe.py')
        self.assertEqual(detail['code'], 'entrypoint_import_failed')
        self.assertEqual(detail['exception_type'], 'RuntimeError')
        self.assertIn('boom', detail['message'])
        self.assertIn('probe.py', detail['traceback'])


class FastFailTestCase(LoaderTestCase):
    def test_an_unimportable_path_fails_before_any_io(self):
        storage = OpenRecordingStorage()
        manifest, digest = seed_revision(storage, STORAGE_KEY, {'probe.py': b'VALUE = 1\n'})
        with self.assertRaises(InvalidModulePathError):
            self.load_again(STORAGE_KEY, digest, 'not valid.py', manifest, storage=storage)
        self.assertEqual(storage.opened, [])
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_an_entrypoint_outside_the_manifest_fails_before_any_io(self):
        storage = OpenRecordingStorage()
        manifest, digest = seed_revision(storage, STORAGE_KEY, {'probe.py': b'VALUE = 1\n'})
        with self.assertRaises(EntrypointImportError) as caught:
            self.load_again(STORAGE_KEY, digest, 'other.py', manifest, storage=storage)
        self.assertEqual(caught.exception.detail['code'], 'entrypoint_not_in_manifest')
        self.assertIsNone(caught.exception.__cause__)
        self.assertEqual(storage.opened, [])
        self.assertEqual(list(self.cache_root.iterdir()), [])

    def test_a_malformed_manifest_is_a_corrupt_revision_not_a_type_error(self):
        # The membership check reads the manifest, so an unvalidated one used to arrive as a
        # raw TypeError or KeyError and take the validation job down with it.
        storage = OpenRecordingStorage()
        digest = compute_digest(manifest_for({'probe.py': b'VALUE = 1\n'}))
        for manifest in (None, {'probe.py': {}}, [{'size': 1, 'sha256': 'a' * 64}]):
            with self.subTest(manifest=manifest), self.assertRaises(RevisionCorruptError):
                self.load_again(STORAGE_KEY, digest, 'probe.py', manifest, storage=storage)
        self.assertEqual(storage.opened, [])
        self.assertEqual(list(self.cache_root.iterdir()), [])


class ConcurrencyTestCase(LoaderTestCase):
    def run_threads(self, targets):
        errors = []

        def wrap(target):
            def call():
                try:
                    target()
                except BaseException as error:
                    errors.append(error)

            return call

        threads = [threading.Thread(target=wrap(target)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])

    def test_concurrent_imports_of_one_entrypoint_yield_one_module(self):
        files = {'__init__.py': ROOT_INIT, 'probe.py': b'VALUE = 1\n'}
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, files)
        results = []
        self.run_threads([lambda: results.append(self.load_again(STORAGE_KEY, digest, 'probe.py', manifest))] * 8)
        self.assertEqual(len(results), 8)
        self.assertEqual(len({id(module) for module in results}), 1)
        self.assertEqual(ROOT_RUNS, ['run'])

    def test_concurrent_first_imports_of_two_projects_both_succeed(self):
        manifest_a, digest_a = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'VALUE = "alpha"\n'})
        manifest_b, digest_b = seed_revision(self.storage, OTHER_KEY, {'probe.py': b'VALUE = "beta"\n'})
        results = {}
        self.run_threads(
            [
                lambda: results.__setitem__('a', self.load_again(STORAGE_KEY, digest_a, 'probe.py', manifest_a)),
                lambda: results.__setitem__('b', self.load_again(OTHER_KEY, digest_b, 'probe.py', manifest_b)),
            ]
        )
        self.assertEqual(results['a'].VALUE, 'alpha')
        self.assertEqual(results['b'].VALUE, 'beta')

    def test_concurrent_first_imports_of_two_revisions_both_succeed(self):
        manifest_a, digest_a = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'VALUE = 1\n'})
        manifest_b, digest_b = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'VALUE = 2\n'})
        results = {}
        self.run_threads(
            [
                lambda: results.__setitem__('a', self.load_again(STORAGE_KEY, digest_a, 'probe.py', manifest_a)),
                lambda: results.__setitem__('b', self.load_again(STORAGE_KEY, digest_b, 'probe.py', manifest_b)),
            ]
        )
        self.assertEqual((results['a'].VALUE, results['b'].VALUE), (1, 2))

    def test_a_session_blocks_a_competitors_unload_until_it_ends(self):
        manifest, digest = seed_revision(self.storage, STORAGE_KEY, {'probe.py': b'VALUE = 1\n'})
        revision_name = revision_module_name(STORAGE_KEY, digest)
        session_open = threading.Event()
        competitor_done = threading.Event()

        def competitor():
            session_open.wait(timeout=10)
            loader.unload_revision(STORAGE_KEY, digest)
            competitor_done.set()

        thread = threading.Thread(target=competitor)
        thread.start()
        with loader.revision_import_session(STORAGE_KEY, digest):
            self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
            session_open.set()
            unload_broke_in = competitor_done.wait(timeout=0.3)
            still_loaded = revision_name in sys.modules
        thread.join(timeout=10)
        self.assertFalse(unload_broke_in)
        self.assertTrue(still_loaded)
        self.assertTrue(competitor_done.is_set())
        self.assertNotIn(revision_name, sys.modules)


class UnloadTestCase(LoaderTestCase):
    def test_unload_returns_the_removed_names_and_spares_neighbors(self):
        files = {'probe.py': b'from . import helper\n', 'helper.py': b'VALUE = 1\n'}
        _, _, digest = self.load(STORAGE_KEY, files, 'probe.py')
        _, _, other_digest = self.load(OTHER_KEY, {'probe.py': b'VALUE = 2\n'}, 'probe.py')
        revision_name = revision_module_name(STORAGE_KEY, digest)
        removed = loader.unload_revision(STORAGE_KEY, digest)
        self.assertEqual(removed, (revision_name, f'{revision_name}.helper', f'{revision_name}.probe'))
        self.assertNotIn(revision_name, sys.modules)
        self.assertFalse(hasattr(sys.modules[project_module_name(STORAGE_KEY)], revision_name.rsplit('.', 1)[1]))
        self.assertIn(revision_module_name(OTHER_KEY, other_digest), sys.modules)

    def test_unloading_a_never_imported_revision_returns_nothing(self):
        never_imported = '6ca13d52ca70c883e0f0bb101e425a89e8624de51db2d2392593af6a84118090'
        self.assertEqual(loader.unload_revision(STORAGE_KEY, never_imported), ())

    def test_an_unloaded_revision_can_be_imported_again(self):
        module, manifest, digest = self.load(STORAGE_KEY, {'probe.py': b'VALUE = 1\n'}, 'probe.py')
        loader.unload_revision(STORAGE_KEY, digest)
        again = self.load_again(STORAGE_KEY, digest, 'probe.py', manifest)
        self.assertIsNot(again, module)
        self.assertEqual(again.VALUE, 1)
