import hashlib
import shutil
import tempfile
import uuid
from pathlib import Path
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import DataFile, DataSource, Job
from netbox_scripts import compat
from netbox_scripts.choices import (
    ActivationPolicyChoices,
    FileDiscoveryStatusChoices,
    ProjectSourceTypeChoices,
    RevisionStatusChoices,
)
from netbox_scripts.ingestion import ingest_data_source
from netbox_scripts.jobs import RevisionValidationJob
from netbox_scripts.models import NetBoxScript, ScriptFile, ScriptProject

FORMS = {
    'named.py': 'from extras.scripts import Script, StringVar\n\n\nclass Named(Script):\n    name = StringVar()\n',
    'wildcard.py': 'from extras.scripts import *\n\n\nclass Wildcard(Script):\n    name = StringVar()\n',
    'aliased.py': (
        'from extras.scripts import Script as Base, StringVar\n\n\nclass Aliased(Base):\n    name = StringVar()\n'
    ),
    'dotted.py': (
        'import extras.scripts\n\n\nclass Dotted(extras.scripts.Script):\n    name = extras.scripts.StringVar()\n'
    ),
    'dotted_alias.py': (
        'import extras.scripts as api\n\n\nclass DottedAlias(api.Script):\n    name = api.StringVar()\n'
    ),
    'from_package.py': (
        'from extras import scripts\n\n\nclass FromPackage(scripts.Script):\n    name = scripts.StringVar()\n'
    ),
    'package_only.py': (
        'import extras\n\n\nclass PackageOnly(extras.scripts.Script):\n    name = extras.scripts.StringVar()\n'
    ),
    'deferred.py': (
        'def _base():\n    from extras.scripts import Script\n\n    return Script\n\n\n'
        'class Deferred(_base()):\n    pass\n'
    ),
}


class DialectTestCase(TestCase):
    """One staged project per case, driven through real ingestion and real validation."""

    def setUp(self):
        # A private cache root per test. The default sits under the shared temporary directory,
        # where a group-writable ancestor makes the runtime tier refuse to import.
        root = Path(tempfile.mkdtemp(prefix='nbcs-dialects-'))
        root.chmod(0o700)
        self.addCleanup(shutil.rmtree, root, True)
        self.enterContext(override_settings(PLUGINS_CONFIG={'netbox_scripts': {'runtime_cache_root': str(root)}}))
        # Enqueueing hands the task to a real queue, and these tests drive the job themselves.
        self.enterContext(mock.patch.object(RevisionValidationJob, 'enqueue_validation', return_value=None))

    def stage_and_validate(self, files, key='dialects', script_files=None):
        """Stage one tree as a Data Source project, drive validation, and return the revision."""
        source = DataSource.objects.create(name=key, type='local', source_url=f'file:///tmp/{key}/')
        project = ScriptProject.objects.create(
            name=key,
            key=key,
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='scripts',
            activation_policy=ActivationPolicyChoices.AUTOMATIC_IF_VALID,
        )
        # Declarations are committed before staging, because staging freezes the enabled ones
        # into the revision's script file snapshot.
        for path in files if script_files is None else script_files:
            ScriptFile.objects.create(project=project, source_path=path, enabled=True)
        for path, source_text in files.items():
            content = source_text.encode()
            DataFile.objects.create(
                source=source,
                path=f'{project.data_path}/{path}',
                size=len(content),
                hash=hashlib.sha256(content).hexdigest(),
                data=content,
                # last_updated is editable=False with no auto_now, so a fixture has to set it.
                last_updated=timezone.now(),
            )
        self.project = project
        revision = ingest_data_source(project).revision
        runner = RevisionValidationJob(Job.objects.create(name='validation', job_id=uuid.uuid4()))
        runner.run(revision_pk=revision.pk, job_id='dialects')
        revision.refresh_from_db()
        return revision

    def test_every_legacy_import_form_publishes(self):
        revision = self.stage_and_validate(FORMS)
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual(revision.validation_errors, [])
        published = sorted(NetBoxScript.objects.filter(project=self.project).values_list('class_name', flat=True))
        self.assertEqual(
            published,
            ['Aliased', 'Deferred', 'Dotted', 'DottedAlias', 'FromPackage', 'Named', 'PackageOnly', 'Wildcard'],
        )

    def test_a_package_root_body_gets_the_redirect_too(self):
        # The root is registered by hand rather than found, so its loader is wrapped separately.
        # If that wrap were missing, RootBase would subclass the host's class and nothing would
        # publish, which makes the published row the discriminator.
        files = {
            '__init__.py': 'from extras.scripts import Script\n\n\nclass RootBase(Script):\n    pass\n',
            'entry.py': 'from . import RootBase\n\n\nclass Rooted(RootBase):\n    pass\n',
        }
        revision = self.stage_and_validate(files, key='rooted', script_files=['entry.py'])
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual([record['class_name'] for record in revision.discovered_scripts], ['Rooted'])

    def test_a_non_legacy_extras_import_still_reaches_netbox(self):
        source = (
            'from extras.scripts import Script\n'
            'from extras.models import Tag\n\n\n'
            'class Tagger(Script):\n'
            '    def run(self, data, commit):\n'
            '        self.log_info(Tag._meta.label)\n'
        )
        revision = self.stage_and_validate({'tagger.py': source}, key='delegated')
        self.assertEqual(revision.status, RevisionStatusChoices.ACTIVE)
        self.assertEqual([record['class_name'] for record in revision.discovered_scripts], ['Tagger'])

    def test_a_dynamic_import_is_the_documented_limit(self):
        # importlib.import_module never consults __import__, so this reaches whatever the host
        # provides and the class is the host's, which discovery does not publish. The revision
        # is refused rather than served empty, and the message names the base it inherited.
        # This is the one case exercising that detection against the real host class.
        source = (
            'import importlib\n\n'
            'Script = importlib.import_module("extras.scripts").Script\n\n\n'
            'class Dynamic(Script):\n'
            '    def run(self, data, commit):\n'
            '        pass\n'
        )
        revision = self.stage_and_validate({'dynamic.py': source}, key='dynamic')
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        self.assertEqual(revision.discovered_scripts, [])
        (failure,) = revision.validation_errors
        self.assertEqual(failure['source_path'], 'dynamic.py')
        self.assertEqual(failure['code'], 'no_scripts_published')
        self.assertIn('"Dynamic" subclasses extras.scripts.Script', failure['message'])
        (module_row,) = ScriptFile.objects.filter(project=revision.project)
        self.assertEqual(module_row.discovery_status, FileDiscoveryStatusChoices.NO_SCRIPTS)

    def test_a_legacy_import_becomes_an_invalid_revision_once_the_host_drops_the_module(self):
        with mock.patch.object(compat, '_host_provides', return_value=False):
            revision = self.stage_and_validate({'named.py': FORMS['named.py']}, key='transition')
        self.assertEqual(revision.status, RevisionStatusChoices.INVALID)
        failure = revision.validation_errors[0]
        self.assertEqual(failure['source_path'], 'named.py')
        self.assertEqual(failure['exception_type'], 'ImportError')
        self.assertIn('netbox_scripts.scripts', failure['message'])
