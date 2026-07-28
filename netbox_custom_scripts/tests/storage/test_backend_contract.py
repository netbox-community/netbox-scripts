"""
Backend contract coverage for the Cloud and Enterprise storage architecture.

Each case drives one full storage lifecycle, staging through activation to job-driven
cleanup, against a backend stripped of a convenience a local filesystem happens to offer.
Together they enforce the hard contract in AGENTS.md: the authoritative store speaks the
ordinary Django storage API only, with no filesystem paths and no directory semantics, so a
deployment may hand it any conforming backend.
"""

import os
import pathlib
import tempfile
import unittest
import uuid
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import InMemoryStorage, Storage
from django.test import TestCase, override_settings

from core.models import Job
from netbox_custom_scripts.choices import RevisionStatusChoices
from netbox_custom_scripts.jobs import ProjectStorageCleanupJob
from netbox_custom_scripts.models import CustomScriptProject
from netbox_custom_scripts.storage import config, service, store
from netbox_custom_scripts.storage.exceptions import RevisionCorruptError
from netbox_custom_scripts.storage.paths import MAX_PATH_BYTES, revision_key, revision_prefix

try:
    import boto3
    from moto import mock_aws
    from storages.backends.s3 import S3Storage  # noqa: F401

    HAS_S3_STACK = True
except ImportError:
    HAS_S3_STACK = False

# S3 support is a hard platform contract. CI sets REQUIRE_S3_TESTS=1 so a missing test stack
# fails the run loudly, while a lightweight local environment still just skips the S3 case.
if os.environ.get('REQUIRE_S3_TESTS') == '1' and not HAS_S3_STACK:
    raise RuntimeError('REQUIRE_S3_TESTS is set and the S3 test stack is not importable. Install the test extras.')

SOURCE = {'deploy.py': b'print("deploy")\n', 'pkg/util.py': b'VALUE = 1\n'}
BASE_STORAGES = {
    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}


class RestrictedStorage(Storage):
    """
    A backend offering only the storage contract Django actually promises.

    A wrapper rather than a subclass, because InMemoryStorage resolves its own internals
    through path(). A backend may do that privately, the contract only forbids the caller
    from needing it, so the refusals sit on the surface this plugin talks to.
    """

    def __init__(self, **kwargs):
        self._inner = InMemoryStorage(**kwargs)

    def open(self, name, mode='rb'):
        return self._inner.open(name, mode)

    def save(self, name, content, max_length=None):
        return self._inner.save(name, content, max_length=max_length)

    def exists(self, name):
        return self._inner.exists(name)

    def delete(self, name):
        self._inner.delete(name)

    def listdir(self, path):
        raise NotImplementedError('This backend has no directory semantics.')

    def path(self, name):
        raise AssertionError(f'Authoritative storage code resolved a filesystem path for "{name}".')


class BackendLifecycleMixin:
    """Drive one full storage lifecycle through the service layer against one backend."""

    def storages_setting(self):
        raise NotImplementedError

    def assert_stored(self, storage_key, digest, paths, present):
        storage = config.get_storage()
        prefix = revision_prefix(storage_key, digest)
        for path in paths:
            self.assertIs(storage.exists(f'{prefix}{path}'), present, f'{prefix}{path}')

    def test_the_full_lifecycle_holds_on_this_backend(self):
        with override_settings(STORAGES={**BASE_STORAGES, 'netbox_custom_scripts': self.storages_setting()}):
            project = CustomScriptProject.objects.create(name='Contract Project', key='contract-project')
            storage_key = str(project.storage_key)

            staged, created = service.stage_revision(project, SOURCE)
            self.assertTrue(created)
            self.assertEqual(staged.status, RevisionStatusChoices.MATERIALIZED)
            digest = staged.digest
            paths = [entry['path'] for entry in staged.manifest]
            self.assert_stored(storage_key, digest, paths, True)

            # Re-staging identical content verifies and reuses the stored tree.
            again, created_again = service.stage_revision(project, SOURCE)
            self.assertFalse(created_again)
            self.assertEqual(again.pk, staged.pk)

            staged.status = RevisionStatusChoices.VALID
            staged.save()
            self.assertEqual(service.activate_revision(staged).status, RevisionStatusChoices.ACTIVE)

            # Deletion hands each revision's cleanup to the job. The enqueue is captured and
            # the job bodies run here, so the lifecycle ends with the backend reclaimed
            # without pushing anything onto a real queue.
            enqueued = []
            with (
                mock.patch.object(
                    ProjectStorageCleanupJob, 'enqueue_cleanup', side_effect=lambda **kwargs: enqueued.append(kwargs)
                ),
                self.captureOnCommitCallbacks(execute=True),
            ):
                project.delete()
            self.assertEqual(len(enqueued), 1)
            for kwargs in enqueued:
                row = Job.objects.create(name='contract-cleanup', job_id=uuid.uuid4())
                ProjectStorageCleanupJob(row).run(job_id='contract', **kwargs)
            self.assert_stored(storage_key, digest, paths, False)


class RestrictedBackendTestCase(BackendLifecycleMixin, TestCase):
    """A backend with no listing and no paths carries the whole lifecycle."""

    def storages_setting(self):
        return {'BACKEND': 'netbox_custom_scripts.tests.storage.test_backend_contract.RestrictedStorage'}


class FileSystemBackendTestCase(BackendLifecycleMixin, TestCase):
    """The lifecycle against real files, the ordinary single-node deployment."""

    def setUp(self):
        super().setUp()
        self.location = self.enterContext(tempfile.TemporaryDirectory(prefix='ncs-contract-'))

    def storages_setting(self):
        return {
            'BACKEND': 'django.core.files.storage.FileSystemStorage',
            'OPTIONS': {'location': self.location},
        }

    def files_on_disk(self):
        return {path for path in pathlib.Path(self.location).rglob('*') if path.is_file()}

    def assert_stored(self, storage_key, digest, paths, present):
        super().assert_stored(storage_key, digest, paths, present)
        # The storage API's answer must be the truth on disk: the exact files while the
        # revision exists, and no file at all once cleanup ran. Empty directories may
        # survive, because the storage API cannot remove one.
        if present:
            self.assertEqual(len(self.files_on_disk()), len(paths))
        else:
            self.assertEqual(self.files_on_disk(), set())


@unittest.skipUnless(HAS_S3_STACK, 'moto and the S3 storage backend are not installed')
class S3BackendTestCase(BackendLifecycleMixin, TestCase):
    """The lifecycle against an S3-compatible object store, mocked by moto."""

    def setUp(self):
        super().setUp()
        self.enterContext(mock_aws())
        boto3.client('s3', region_name='us-east-1').create_bucket(Bucket='netbox-custom-scripts-test')

    def storages_setting(self):
        return {
            'BACKEND': 'storages.backends.s3.S3Storage',
            'OPTIONS': {
                'bucket_name': 'netbox-custom-scripts-test',
                'region_name': 'us-east-1',
                'access_key': 'testing',
                'secret_key': 'testing',
                'location': 'custom-scripts',
            },
        }

    def test_an_oversized_object_is_rejected_without_a_download(self):
        # This backend downloads an object whole when it is opened, so verification has to
        # reject a wrong-size replacement from its HEAD metadata, before any open happens.
        with override_settings(STORAGES={**BASE_STORAGES, 'netbox_custom_scripts': self.storages_setting()}):
            project = CustomScriptProject.objects.create(name='Oversize Project', key='oversize-project')
            staged, _ = service.stage_revision(project, SOURCE)
            storage = config.get_storage()
            key = revision_key(project.storage_key, staged.digest, 'deploy.py')
            storage.delete(key)
            storage.save(key, ContentFile(SOURCE['deploy.py'] * 4096))
            opened = []
            original = storage.open
            with (
                mock.patch.object(
                    storage, 'open', side_effect=lambda name, mode='rb': opened.append(name) or original(name, mode)
                ),
                self.assertRaises(RevisionCorruptError) as ctx,
            ):
                store.verify_revision_tree(storage, project.storage_key, staged.digest, staged.manifest)
            self.assertEqual(sorted(ctx.exception.reasons), ['size_mismatch:deploy.py'])
            self.assertNotIn(key, opened)

    def test_a_boundary_length_path_fits_the_complete_key_budget(self):
        # The documented S3 configuration carries a location prefix, and S3 bounds the
        # complete key at 1024 UTF-8 bytes, so the longest accepted source path has to fit
        # with the plugin prefix and the location in front of it.
        segments = ['d' * 200, 'e' * 200, 'f' * 200]
        tail = 'x' * (MAX_PATH_BYTES - sum(len(segment) + 1 for segment in segments) - 3) + '.py'
        path = '/'.join([*segments, tail])
        self.assertEqual(len(path.encode('utf-8')), MAX_PATH_BYTES)
        with override_settings(STORAGES={**BASE_STORAGES, 'netbox_custom_scripts': self.storages_setting()}):
            project = CustomScriptProject.objects.create(name='Budget Project', key='budget-project')
            staged, _ = service.stage_revision(project, {path: b'BUDGET = 1\n'})
            self.assertEqual(staged.status, RevisionStatusChoices.MATERIALIZED)
            key = revision_key(project.storage_key, staged.digest, path)
            self.assertLessEqual(len(f'custom-scripts/{key}'.encode()), 1024)
