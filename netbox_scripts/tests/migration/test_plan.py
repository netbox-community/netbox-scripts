import hashlib
import shutil
import tempfile

from django.core.exceptions import SuspiciousFileOperation
from django.core.files.base import ContentFile
from django.core.files.storage import storages
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.choices import JobStatusChoices, ManagedFileRootPathChoices
from core.models import DataFile, DataSource
from extras.models import ScriptModule
from netbox_scripts.choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from netbox_scripts.jobs import MigrationInventoryJob
from netbox_scripts.migration import dialects, plan, source
from netbox_scripts.migration.source import LegacyModule, LegacyScript
from netbox_scripts.models import ScriptProject

LEGACY_SCRIPT = b"""from extras.scripts import Script


class Deploy(Script):
    def run(self, data, commit):
        pass
"""

NATIVE_SCRIPT = b"""from netbox_scripts.scripts import Script


class Provision(Script):
    def run(self, data, commit):
        pass
"""


# A module that declares a Script row and defines the class behind it, which is the only shape
# the built-in feature actually produces.
NATIVE_A = b"""from netbox_scripts.scripts import Script


class A(Script):
    def run(self, data, commit):
        pass
"""


def legacy(pk, file_path, data_source_id=None, data_path='', file_root='scripts', scripts=()):
    return LegacyModule(
        pk=pk,
        file_root=file_root,
        file_path=file_path,
        python_name=file_path.removesuffix('.py'),
        data_source_id=data_source_id,
        data_path=data_path,
        scripts=tuple(
            LegacyScript(pk=script_pk, name=name, is_executable=(rest[0] if rest else True))
            for script_pk, name, *rest in scripts
        ),
    )


class ReportExclusionTestCase(TestCase):
    """Reports are not covered, and the inventory says so rather than failing on them."""

    def setUp(self):
        self.source = DataSource.objects.create(name='Repo', type='local', source_url='file:///tmp/repo')

    def module(self, path, root):
        content = b'x = 1\n'
        data_file = DataFile.objects.create(
            source=self.source,
            path=path,
            size=len(content),
            hash=hashlib.sha256(content).hexdigest(),
            data=content,
            last_updated=timezone.now(),
        )
        module = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=data_file)
        module.full_clean()
        module.save()
        if root != ManagedFileRootPathChoices.SCRIPTS:
            ScriptModule.objects.filter(pk=module.pk).update(file_root=root)
        return module

    def test_a_report_is_not_read_and_does_not_block(self):
        # Its bytes are under REPORTS_ROOT, so including it produced an unreadable blocking finding.
        self.module('deploy.py', ManagedFileRootPathChoices.SCRIPTS)
        report = self.module('audit.py', ManagedFileRootPathChoices.REPORTS)

        keys = [module.pk for module in source.legacy_modules()]

        self.assertNotIn(report.pk, keys)

    def test_the_inventory_counts_the_reports_and_says_what_reaches_them(self):
        self.module('audit.py', ManagedFileRootPathChoices.REPORTS)

        report = plan.build_report()

        self.assertEqual(report['reports'], 1)
        codes = {finding['code']: finding['level'] for finding in report['findings']}
        self.assertEqual(codes['reports_excluded'], plan.WARNING)
        # The warning is where an operator first reads what happens to reports, so what it
        # claims is pinned rather than only its code.
        message = next(f['message'] for f in report['findings'] if f['code'] == 'reports_excluded')
        self.assertIn('not part of this migration', message)
        self.assertIn('extras.scriptmodule', message)
        self.assertIn('for good', message)
        self.assertNotEqual(report['status'], plan.BLOCKING)


class GroupTestCase(SimpleTestCase):
    """One Project per folder that holds scripts, and one per uploaded module."""

    def test_modules_in_one_directory_share_a_project(self):
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='automation/netbox/deploy.py'),
            legacy(2, 'remove.py', data_source_id=7, data_path='automation/netbox/remove.py'),
        ]
        proposed = plan.group(modules)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0].data_path, 'automation/netbox')
        self.assertEqual(proposed[0].module_pks, (1, 2))

    def test_separate_directories_become_separate_projects(self):
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='automation/netbox/deploy.py'),
            legacy(2, 'audit.py', data_source_id=7, data_path='reporting/audit.py'),
        ]
        self.assertEqual({item.data_path for item in plan.group(modules)}, {'automation/netbox', 'reporting'})

    def test_same_directory_on_two_sources_stays_separate(self):
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='scripts/deploy.py'),
            legacy(2, 'deploy.py', data_source_id=8, data_path='scripts/deploy.py'),
        ]
        self.assertEqual(len(plan.group(modules)), 2)

    def test_a_module_at_the_repository_root_groups_at_the_empty_path(self):
        proposed = plan.group([legacy(1, 'deploy.py', data_source_id=7, data_path='deploy.py')])
        self.assertEqual(proposed[0].data_path, '')

    def test_each_uploaded_module_gets_its_own_project(self):
        proposed = plan.group([legacy(1, 'a.py'), legacy(2, 'b.py')])
        self.assertEqual(len(proposed), 2)
        self.assertTrue(all(item.source_type == ProjectSourceTypeChoices.UPLOAD for item in proposed))
        self.assertTrue(all(item.data_path == '' for item in proposed))

    def test_a_nested_folder_joins_the_project_above_it(self):
        # The model refuses two projects whose data paths overlap on one data source, and a
        # project's tree already holds its subdirectories, so the deeper folder joins the higher.
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='automation/deploy.py'),
            legacy(2, 'audit.py', data_source_id=7, data_path='automation/netbox/audit.py'),
        ]
        proposed = plan.group(modules)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0].data_path, 'automation')
        self.assertEqual(proposed[0].module_pks, (1, 2))

    def test_a_module_at_the_root_absorbs_every_folder_on_that_source(self):
        modules = [
            legacy(1, 'top.py', data_source_id=7, data_path='top.py'),
            legacy(2, 'deploy.py', data_source_id=7, data_path='automation/deploy.py'),
        ]
        proposed = plan.group(modules)
        self.assertEqual(len(proposed), 1)
        self.assertEqual(proposed[0].data_path, '')
        self.assertEqual(proposed[0].module_pks, (1, 2))

    def test_sibling_folders_do_not_merge(self):
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='automation/a.py'),
            legacy(2, 'b.py', data_source_id=7, data_path='automation-old/b.py'),
        ]
        self.assertEqual(len(plan.group(modules)), 2)

    def test_an_overlapping_folder_on_another_source_does_not_merge(self):
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='automation/a.py'),
            legacy(2, 'b.py', data_source_id=8, data_path='automation/netbox/b.py'),
        ]
        self.assertEqual(len(plan.group(modules)), 2)

    def test_keys_are_deterministic_and_unique(self):
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='automation/netbox/deploy.py'),
            legacy(2, 'audit.py', data_source_id=7, data_path='reporting/audit.py'),
            legacy(3, 'solo.py'),
        ]
        first = [item.key for item in plan.group(modules)]
        second = [item.key for item in plan.group(list(reversed(modules)))]
        self.assertEqual(sorted(first), sorted(second))
        self.assertEqual(len(set(first)), 3)

    def test_keys_fit_the_model_field(self):
        long_path = '/'.join(['segment'] * 30) + '/deploy.py'
        proposed = plan.group([legacy(1, 'deploy.py', data_source_id=7, data_path=long_path)])
        self.assertLessEqual(len(proposed[0].key), 100)


class FindingsTestCase(SimpleTestCase):
    """Findings an operator has to act on before a migration."""

    def test_a_hyphenated_filename_blocks(self):
        # The community collection holds five real reports refused for exactly this.
        modules = [legacy(1, 'my-report.py', data_source_id=7, data_path='scripts/my-report.py')]
        report = plan.build_report(modules=modules, read=lambda module: b'')
        codes = {finding['code']: finding['level'] for finding in report['findings']}
        self.assertEqual(codes['not_importable'], plan.BLOCKING)

    def test_a_hyphenated_intermediate_folder_blocks(self):
        # The basename is a fine identifier while the path it is staged at is not.
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='scripts/a.py'),
            legacy(2, 'b.py', data_source_id=7, data_path='scripts/custom-scripts/b.py'),
        ]

        report = plan.build_report(modules=modules, read=lambda module: b'')

        self.assertEqual(report['status'], plan.BLOCKING)
        blocking = [f for f in report['findings'] if f['code'] == 'not_importable']
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['pk'], 2)
        self.assertIn('custom-scripts/b', blocking[0]['message'])

    def test_a_nested_folder_that_stays_importable_does_not_block(self):
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='scripts/a.py', scripts=[(11, 'A')]),
            legacy(2, 'b.py', data_source_id=7, data_path='scripts/nested/b.py', scripts=[(12, 'B')]),
        ]
        bodies = {1: NATIVE_A, 2: NATIVE_A.replace(b'class A', b'class B')}

        report = plan.build_report(modules=modules, read=lambda module: bodies[module.pk])

        self.assertEqual([f['code'] for f in report['findings']], [])

    def test_a_module_at_the_data_source_root_blocks(self):
        # Grouping still collapses to the root, and the model refuses to stage it, so the
        # inventory has to say so rather than let staging raise.
        modules = [
            legacy(1, 'top.py', data_source_id=7, data_path='top.py'),
            legacy(2, 'deploy.py', data_source_id=7, data_path='automation/deploy.py'),
        ]

        report = plan.build_report(modules=modules, read=lambda module: b'')

        self.assertEqual(report['status'], plan.BLOCKING)
        blocking = [f for f in report['findings'] if f['code'] == 'data_source_root']
        self.assertEqual(len(blocking), 1)
        self.assertIsNone(blocking[0]['pk'])
        self.assertIn('2 module(s)', blocking[0]['message'])
        self.assertIn(report['projects'][0]['key'], blocking[0]['message'])

    def test_an_import_nothing_provides_blocks(self):
        modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
        body = b'import nonexistent_vendor_sdk\n\n\nclass A:\n    def run(self):\n        pass\n'

        report = plan.build_report(modules=modules, read=lambda module: body)

        blocking = [f for f in report['findings'] if f['code'] == 'import_unresolvable']
        self.assertEqual(len(blocking), 1)
        self.assertIn('nonexistent_vendor_sdk', blocking[0]['message'])
        self.assertEqual(report['status'], plan.BLOCKING)

    def test_a_standard_library_import_resolves(self):
        modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
        body = b'import os\nimport json\n\n\nclass A:\n    def run(self):\n        pass\n'

        report = plan.build_report(modules=modules, read=lambda module: body)

        self.assertEqual([f for f in report['findings'] if f['code'] == 'import_unresolvable'], [])

    def test_an_absolute_sibling_import_still_blocks(self):
        # It never reached the sibling under the built-in feature and it will not after migration,
        # so treating a proposed neighbour as a resolution would hide the dead end.
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='scripts/a.py', scripts=[(11, 'A')]),
            legacy(2, 'helpers.py', data_source_id=7, data_path='scripts/helpers.py'),
        ]
        bodies = {1: b'import helpers\n\n\nclass A:\n    def run(self):\n        pass\n', 2: b'X = 1\n'}

        report = plan.build_report(modules=modules, read=lambda module: bodies[module.pk])

        blocking = [f for f in report['findings'] if f['code'] == 'import_unresolvable']
        self.assertEqual(len(blocking), 1)
        self.assertEqual(blocking[0]['pk'], 1)

    def test_an_import_the_module_guards_itself_does_not_block(self):
        # A guarded optional import is an ordinary idiom, and blocking a whole migration over one
        # would be worse than the dead end this check exists to prevent.
        modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
        body = (
            b'try:\n    import optional_vendor_sdk\nexcept ImportError:\n    optional_vendor_sdk = None\n'
            b'\n\nclass A:\n    def run(self):\n        pass\n'
        )

        report = plan.build_report(modules=modules, read=lambda module: body)

        self.assertEqual([f for f in report['findings'] if f['code'] == 'import_unresolvable'], [])

    def test_an_import_inside_a_function_body_does_not_block(self):
        # It runs when the method runs, not at import time, so the module imports and reaches a
        # verdict exactly as it did under the built-in feature.
        modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
        body = (
            b'from netbox_scripts.scripts import Script\n\n\n'
            b'class A(Script):\n    def run(self, data, commit):\n        import pynetbox_absent\n'
        )

        report = plan.build_report(modules=modules, read=lambda module: body)

        self.assertEqual([f for f in report['findings'] if f['code'] == 'import_unresolvable'], [])

    def test_a_fallback_import_in_the_handler_is_not_guarded(self):
        # The try protects its own body. A name imported in the except clause has nothing
        # catching it, so it is exactly the import that would fail at load time.
        modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
        body = (
            b'try:\n    import json\nexcept ImportError:\n    import absent_fallback_package\n'
            b'\n\nclass A:\n    def run(self):\n        pass\n'
        )

        report = plan.build_report(modules=modules, read=lambda module: body)

        blocking = [f for f in report['findings'] if f['code'] == 'import_unresolvable']
        self.assertEqual(len(blocking), 1)
        self.assertIn('absent_fallback_package', blocking[0]['message'])

    def test_a_relative_sibling_import_resolves(self):
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='scripts/a.py', scripts=[(11, 'A')]),
            legacy(2, 'helpers.py', data_source_id=7, data_path='scripts/helpers.py'),
        ]
        bodies = {1: b'from . import helpers\n\n\nclass A:\n    def run(self):\n        pass\n', 2: b'X = 1\n'}

        report = plan.build_report(modules=modules, read=lambda module: bodies[module.pk])

        self.assertEqual([f for f in report['findings'] if f['code'] == 'import_unresolvable'], [])

    def test_a_re_exported_script_warns_that_it_will_not_publish(self):
        # The plugin publishes only a class the module itself defines or names in script_order.
        modules = [
            legacy(1, 'a.py', data_source_id=7, data_path='scripts/a.py', scripts=[(11, 'Deploy')]),
            legacy(2, 'shared.py', data_source_id=7, data_path='scripts/shared.py'),
        ]
        bodies = {1: b'from .shared import Deploy\n', 2: b'class Deploy:\n    def run(self):\n        pass\n'}

        report = plan.build_report(modules=modules, read=lambda module: bodies[module.pk])

        warnings = [f for f in report['findings'] if f['code'] == 'script_not_defined_here']
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]['pk'], 1)
        self.assertIn('Deploy', warnings[0]['message'])
        self.assertEqual(warnings[0]['level'], plan.WARNING)

    def test_an_uploaded_path_the_store_refuses_blocks(self):
        # The inventory has to apply staging's own path policy, or staging raises where it reported clean.
        modules = [legacy(1, 'a' * 800 + '.py')]

        report = plan.build_report(modules=modules, read=lambda module: b'X = 1\n')

        blocking = [f for f in report['findings'] if f['code'] == 'not_importable']
        self.assertEqual(len(blocking), 1)
        self.assertEqual(report['status'], plan.BLOCKING)

    def test_the_helper_warning_is_silent_where_a_module_is_refused_outright(self):
        # Each of these bodies is refused outright, so none of them migrates at all.
        cases = {
            'report_style': b'class R:\n    def test_a(self):\n        pass\n',
            'unparsable': b'def broken(\n',
            'import_unresolvable': b'import nope_not_here_at_all\n',
        }
        for code, body in cases.items():
            with self.subTest(code=code):
                report = plan.build_report(modules=[legacy(1, 'a.py')], read=lambda module, body=body: body)
                codes = [f['code'] for f in report['findings']]
                self.assertIn(code, codes)
                self.assertNotIn('publishes_nothing', codes)

    def test_the_helper_warning_is_silent_for_a_path_that_cannot_be_staged(self):
        report = plan.build_report(modules=[legacy(1, 'my-report.py')], read=lambda module: b'X = 1\n')

        codes = [f['code'] for f in report['findings']]
        self.assertIn('not_importable', codes)
        self.assertNotIn('publishes_nothing', codes)

    def test_report_style_blocks_and_legacy_import_warns(self):
        modules = [legacy(1, 'r.py'), legacy(2, 'l.py')]
        bodies = {1: b'class R:\n    def test_a(self):\n        pass\n', 2: b'from extras.scripts import Script\n'}
        report = plan.build_report(modules=modules, read=lambda module: bodies[module.pk])
        codes = {finding['code']: finding['level'] for finding in report['findings']}
        self.assertEqual(codes['report_style'], plan.BLOCKING)
        self.assertEqual(codes['legacy_import'], plan.WARNING)
        self.assertEqual(report['dialects']['report_style'], 1)
        self.assertEqual(report['dialects']['legacy_import'], 1)

    def test_unparsable_source_blocks(self):
        report = plan.build_report(modules=[legacy(1, 'a.py')], read=lambda module: b'def broken(\n')
        codes = {finding['code']: finding['level'] for finding in report['findings']}
        self.assertEqual(codes['unparsable'], plan.BLOCKING)

    def test_a_native_module_produces_no_finding(self):
        body = NATIVE_A
        report = plan.build_report(modules=[legacy(1, 'a.py', scripts=[(11, 'A')])], read=lambda module: body)
        self.assertEqual(report['findings'], [])
        self.assertEqual(report['dialects']['native'], 1)

    def test_unreadable_source_is_a_finding_rather_than_a_crash(self):
        def refuse(module):
            raise OSError('gone')

        report = plan.build_report(modules=[legacy(1, 'a.py')], read=refuse)
        self.assertEqual({f['code'] for f in report['findings']}, {'source_unreadable'})

    def test_a_report_root_outside_the_scripts_backend_is_a_finding(self):
        # file_root 'reports' maps to REPORTS_ROOT, which the scripts backend may refuse to open.
        def refuse(module):
            raise SuspiciousFileOperation('outside the base path')

        report = plan.build_report(modules=[legacy(1, 'a.py', file_root='reports')], read=refuse)
        self.assertEqual({f['code'] for f in report['findings']}, {'source_unreadable'})

    def test_status_is_the_worst_finding_level(self):
        bodies = {
            plan.READY: NATIVE_A,
            plan.WARNING: NATIVE_A.replace(b'netbox_scripts.scripts', b'extras.scripts'),
            plan.BLOCKING: b'class A:\n    def test_a(self):\n        pass\n',
        }
        for expected, body in bodies.items():
            with self.subTest(status=expected):
                modules = [legacy(1, 'a.py', scripts=[(11, 'A')])]
                report = plan.build_report(modules=modules, read=lambda module, body=body: body)
                self.assertEqual(report['status'], expected)

    def test_a_module_publishing_nothing_is_reported_as_a_helper(self):
        # It migrates as a file rather than a script file, which has to be visible before staging.
        modules = [legacy(1, 'deploy.py', scripts=[(11, 'Deploy')]), legacy(2, 'util.py')]

        report = plan.build_report(modules=modules, read=lambda module: b'def describe():\n    return 1\n')

        finding = next(item for item in report['findings'] if item['code'] == 'publishes_nothing')
        self.assertEqual(finding['level'], plan.WARNING)
        self.assertEqual(finding['pk'], 2)
        self.assertIn('helper file', finding['message'])
        self.assertEqual(report['status'], plan.WARNING)

    def test_a_module_whose_class_left_the_file_is_reported_too(self):
        # A soft-deleted row publishes nothing and never will, so it counts for nothing here.
        modules = [legacy(1, 'gone.py', scripts=[(11, 'Gone', False)])]

        report = plan.build_report(modules=modules, read=lambda module: b'x = 1\n')

        self.assertTrue(any(item['code'] == 'publishes_nothing' for item in report['findings']))

    def test_source_this_plugin_would_publish_from_is_not_called_a_helper(self):
        # No built-in row, because the feature only recorded a subclass of its own base, but the
        # class is there and the plugin publishes it.
        body = (
            b'from netbox_scripts.scripts import Script\n\n\n'
            b'class P(Script):\n    def run(self, data, commit):\n        pass\n'
        )
        report = plan.build_report(modules=[legacy(1, 'p.py')], read=lambda module: body)

        self.assertFalse(any(item['code'] == 'publishes_nothing' for item in report['findings']))

    def test_a_report_over_supplied_modules_reads_no_database(self):
        report = plan.build_report(modules=[legacy(1, 'a.py')], read=lambda module: b'')
        self.assertEqual(report['references'], {})
        self.assertEqual(len(report['projects']), 1)


class MigrationInventoryJobTestCase(TestCase):
    """The inventory pass end to end, against real legacy rows."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A directory rather than an in-memory store, because ManagedFile writes through its own
        # storage instance while the loader reads the cached one.
        scripts_root = tempfile.mkdtemp(prefix='legacy-scripts-')
        cls.addClassCleanup(shutil.rmtree, scripts_root, ignore_errors=True)
        cls.enterClassContext(
            override_settings(
                STORAGES={
                    'default': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
                    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
                    'scripts': {
                        'BACKEND': 'django.core.files.storage.FileSystemStorage',
                        'OPTIONS': {'location': scripts_root, 'allow_overwrite': True},
                    },
                    'netbox_scripts': {'BACKEND': 'django.core.files.storage.InMemoryStorage'},
                }
            )
        )

    def setUp(self):
        data_source = DataSource.objects.create(name='Automation', type='local', source_url='file:///tmp/automation')
        # last_updated is editable=False with no auto_now, so a fixture has to set it.
        data_file = DataFile.objects.create(
            source=data_source,
            path='automation/deploy.py',
            size=len(LEGACY_SCRIPT),
            hash=hashlib.sha256(LEGACY_SCRIPT).hexdigest(),
            data=LEGACY_SCRIPT,
            last_updated=timezone.now(),
        )
        synced = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=data_file)
        synced.full_clean()
        synced.save()
        storages['scripts'].save('provision.py', ContentFile(NATIVE_SCRIPT))
        ScriptModule.objects.create(file_path='provision.py')

    def test_the_report_names_every_module_and_its_dialect(self):
        job = MigrationInventoryJob.enqueue(immediate=True)
        job.refresh_from_db()
        self.assertEqual(job.status, JobStatusChoices.STATUS_COMPLETED)
        report = job.data
        self.assertEqual(
            {entry['path']: entry['dialect'] for entry in report['modules']},
            {'automation/deploy.py': dialects.LEGACY_IMPORT, 'provision.py': dialects.NATIVE},
        )
        self.assertEqual(report['status'], plan.WARNING)
        self.assertEqual(report['references']['event_rules'], 0)

    def test_the_pass_proposes_two_projects_and_creates_none(self):
        job = MigrationInventoryJob.enqueue(immediate=True)
        job.refresh_from_db()
        proposed = job.data['projects']
        self.assertEqual(len(proposed), 2)
        self.assertEqual(
            {item['source_type'] for item in proposed},
            {ProjectSourceTypeChoices.DATA_SOURCE, ProjectSourceTypeChoices.UPLOAD},
        )
        self.assertFalse(ScriptProject.objects.exists())


class ExistingProjectTestCase(TestCase):
    """The inventory reads the Script Projects an operator already made, which staging reuses."""

    def setUp(self):
        self.source = DataSource.objects.create(name='Repo', type='local', source_url='file:///tmp/repo')

    def legacy_module(self, path):
        """Create one built-in script module fed by a file on the source."""
        data_file = DataFile.objects.create(
            source=self.source,
            path=path,
            size=len(NATIVE_SCRIPT),
            hash=hashlib.sha256(NATIVE_SCRIPT).hexdigest(),
            data=NATIVE_SCRIPT,
            last_updated=timezone.now(),
        )
        module = ScriptModule(file_root=ManagedFileRootPathChoices.SCRIPTS, data_file=data_file)
        module.full_clean()
        module.save()
        return module

    def project(self, data_path, policy):
        """Create a Script Project an operator would have made by hand."""
        return ScriptProject.objects.create(
            name=f'existing {data_path}',
            key=f'existing-{data_path.replace("/", "-")}',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=self.source,
            data_path=data_path,
            activation_policy=policy,
        )

    @staticmethod
    def codes(report):
        return {finding['code'] for finding in report['findings']}

    def test_a_reused_project_on_an_automatic_policy_blocks(self):
        # Staging would declare on it and validation would then put it into service.
        self.legacy_module('scripts/deploy.py')
        self.project('scripts', ActivationPolicyChoices.AUTOMATIC_IF_VALID)

        report = plan.build_report()

        self.assertIn('project_not_manual', self.codes(report))
        self.assertEqual(report['status'], plan.BLOCKING)

    def test_a_project_overlapping_the_proposed_path_blocks(self):
        # The model refuses two overlapping data paths on one source, so staging would raise.
        self.legacy_module('scripts/deploy/run.py')
        self.project('scripts', ActivationPolicyChoices.MANUAL)

        report = plan.build_report()

        self.assertIn('project_conflict', self.codes(report))
        self.assertEqual(report['status'], plan.BLOCKING)

    def test_a_reused_project_on_the_manual_policy_is_the_supported_case(self):
        self.legacy_module('scripts/deploy.py')
        self.project('scripts', ActivationPolicyChoices.MANUAL)

        report = plan.build_report()

        self.assertNotIn('project_not_manual', self.codes(report))
        self.assertNotIn('project_conflict', self.codes(report))
