import sys
import tempfile
import uuid
from pathlib import Path

from django.test import TestCase, override_settings

from core.models import Job
from netbox_scripts.choices import RevisionStatusChoices
from netbox_scripts.models import ScriptFile, ScriptProject
from netbox_scripts.runtime.exceptions import ScriptMetadataError, ScriptResolutionError
from netbox_scripts.runtime.loader import revision_import_session, unload_revision
from netbox_scripts.runtime.naming import PRIVATE_ROOT, revision_module_name
from netbox_scripts.runtime.resolution import resolve_script_class
from netbox_scripts.scripts import Script
from netbox_scripts.storage import config, service
from netbox_scripts.tests.runtime.test_cache import discard_tree
from netbox_scripts.tests.storage.test_service import IN_MEMORY_STORAGES
from netbox_scripts.validation import validate_revision


def script_source(class_name):
    """Return entrypoint source publishing one Script subclass."""
    return f'from netbox_scripts.scripts import Script\n\n\nclass {class_name}(Script):\n    pass\n'.encode()


class ResolutionTestMixin:
    def setUp(self):
        super().setUp()
        self.enterContext(override_settings(STORAGES=IN_MEMORY_STORAGES))
        self.cache_root = Path(tempfile.mkdtemp())
        self.enterContext(
            override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(self.cache_root)}})
        )
        self.addCleanup(discard_tree, self.cache_root)
        self.addCleanup(self._purge_namespace)
        self.project = ScriptProject.objects.create(name='Runnable Project', key='runnable-project')

    def _purge_namespace(self):
        for name in [n for n in sys.modules if n == PRIVATE_ROOT or n.startswith(f'{PRIVATE_ROOT}.')]:
            del sys.modules[name]

    def validated(self, files, entrypoints):
        """Stage one tree, drive it to a verdict, and return the refreshed revision."""
        for source_path in entrypoints:
            ScriptFile.objects.create(project=self.project, source_path=source_path, enabled=True)
        revision, _ = service.stage_revision(self.project, files)
        job = Job.objects.create(name='resolution-test', job_id=uuid.uuid4())
        return validate_revision(revision, job=job)

    def resolve(self, revision, module_path, class_name, discovered_scripts=None):
        """Resolve one identity against a revision, holding the import session the caller owns."""
        storage_key = str(revision.project.storage_key)
        with revision_import_session(storage_key, revision.digest):
            self.addCleanup(unload_revision, storage_key, revision.digest)
            return resolve_script_class(
                storage_key,
                revision.digest,
                discovered_scripts=(revision.discovered_scripts if discovered_scripts is None else discovered_scripts),
                project_key=revision.project.key,
                module_path=module_path,
                class_name=class_name,
                storage=config.get_storage(),
                manifest=revision.manifest,
            )


class ResolveScriptClassTestCase(ResolutionTestMixin, TestCase):
    def test_a_published_identity_resolves_to_its_class(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)

        resolved = self.resolve(revision, 'deploy', 'Deploy')

        self.assertTrue(issubclass(resolved, Script))
        self.assertEqual(resolved.__name__, 'Deploy')

    def test_the_resolved_class_carries_its_user_facing_identity(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])

        resolved = self.resolve(revision, 'deploy', 'Deploy')

        # Discovery stamps the logical module, so the private namespace never reaches a caller.
        self.assertEqual(resolved.module, 'deploy')
        self.assertEqual(resolved.full_name, 'deploy.Deploy')

    def test_a_class_published_through_script_order_resolves_through_its_entrypoint(self):
        revision = self.validated(
            {
                'helpers.py': script_source('Shared'),
                'deploy.py': b'from .helpers import Shared\n\nscript_order = [Shared]\n',
            },
            ['deploy.py'],
        )
        self.assertEqual(revision.status, RevisionStatusChoices.VALID)
        record = revision.discovered_scripts[0]
        # The defining module has no declaration of its own, which is why the snapshot has to
        # record the entrypoint that surfaced the class.
        self.assertEqual(record['module_path'], 'helpers')
        self.assertEqual(record['entrypoint_path'], 'deploy.py')

        resolved = self.resolve(revision, 'helpers', 'Shared')

        self.assertEqual(resolved.__name__, 'Shared')
        self.assertEqual(resolved.module, 'helpers')

    def test_an_identity_absent_from_the_snapshot_is_refused(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])

        with self.assertRaises(ScriptResolutionError) as caught:
            self.resolve(revision, 'deploy', 'Missing')

        self.assertEqual(caught.exception.code, 'not_published')
        self.assertEqual(caught.exception.name, 'deploy.Missing')

    def test_an_identity_the_entrypoint_no_longer_publishes_is_refused(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])
        # A snapshot that is well formed but names a class the entrypoint does not define.
        stale = [dict(revision.discovered_scripts[0], module_path='deploy', class_name='Ghost')]

        with self.assertRaises(ScriptResolutionError) as caught:
            self.resolve(revision, 'deploy', 'Ghost', discovered_scripts=stale)

        self.assertEqual(caught.exception.code, 'no_longer_published')

    def test_a_snapshot_that_could_not_have_been_built_is_refused(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])
        tampered = [dict(revision.discovered_scripts[0], position=7)]

        with self.assertRaises(ScriptMetadataError):
            self.resolve(revision, 'deploy', 'Deploy', discovered_scripts=tampered)

    def test_resolution_leaves_the_revision_loaded_for_the_caller(self):
        revision = self.validated({'deploy.py': script_source('Deploy')}, ['deploy.py'])
        prefix = revision_module_name(str(self.project.storage_key), revision.digest)

        self.resolve(revision, 'deploy', 'Deploy')

        # The caller has to run the class after resolving it, so the module stays imported.
        self.assertIn(f'{prefix}.deploy', sys.modules)
