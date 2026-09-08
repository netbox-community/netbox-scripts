from django.test import SimpleTestCase, TestCase

from core.models import DataSource
from netbox_scripts.choices import ProjectSourceTypeChoices
from netbox_scripts.migration import mapping
from netbox_scripts.migration.source import LegacyModule, LegacyScript
from netbox_scripts.models import NetBoxScript, ScriptProject


def legacy(pk, file_path, data_source_id=None, data_path='', scripts=()):
    """One built-in module as the read seam would report it."""
    return LegacyModule(
        pk=pk,
        file_root='scripts',
        file_path=file_path,
        python_name=file_path.removesuffix('.py'),
        data_source_id=data_source_id,
        data_path=data_path,
        scripts=tuple(LegacyScript(pk=script_pk, name=name) for script_pk, name in scripts),
    )


class BuildMapTestCase(SimpleTestCase):
    """The derived identity, with no plugin row in sight."""

    def test_a_nested_module_maps_onto_the_collapsed_project(self):
        # The deeper folder joins the shallower one, so its path inside that project keeps the
        # segment the collapse absorbed.
        modules = [
            legacy(1, 'deploy.py', data_source_id=7, data_path='automation/deploy.py', scripts=[(11, 'Deploy')]),
            legacy(2, 'audit.py', data_source_id=7, data_path='automation/netbox/audit.py', scripts=[(12, 'Audit')]),
        ]

        result = mapping.build_map(modules)

        self.assertEqual([entry['source_path'] for entry in result['modules']], ['deploy.py', 'netbox/audit.py'])
        self.assertEqual([entry['module_path'] for entry in result['scripts']], ['deploy', 'netbox.audit'])
        self.assertEqual(len({entry['project_key'] for entry in result['modules']}), 1)
        self.assertEqual(result['unmapped'], [])

    def test_an_uploaded_module_maps_onto_a_project_of_its_own(self):
        modules = [
            legacy(1, 'oneoff.py', scripts=[(11, 'OneOff')]),
            legacy(2, 'other.py', scripts=[(12, 'Other')]),
        ]

        result = mapping.build_map(modules)

        self.assertEqual([entry['source_path'] for entry in result['modules']], ['oneoff.py', 'other.py'])
        self.assertEqual(len(mapping.project_keys(result)), 2)

    def test_every_class_in_one_module_maps_to_the_same_module_path(self):
        modules = [legacy(1, 'deploy.py', scripts=[(11, 'Deploy'), (12, 'Remove')])]

        result = mapping.build_map(modules)

        self.assertEqual([entry['class_name'] for entry in result['scripts']], ['Deploy', 'Remove'])
        self.assertEqual({entry['module_path'] for entry in result['scripts']}, {'deploy'})
        self.assertEqual([entry['legacy_pk'] for entry in result['scripts']], [11, 12])
        self.assertEqual({entry['legacy_module_pk'] for entry in result['scripts']}, {1})

    def test_the_legacy_class_name_is_carried_through_unchanged(self):
        modules = [legacy(1, 'deploy.py', scripts=[(11, 'Deploy')])]

        entry = mapping.build_map(modules)['scripts'][0]

        self.assertEqual(entry['legacy_name'], entry['class_name'])

    def test_a_path_no_loader_could_import_is_reported_rather_than_raised(self):
        # A hyphen is not a Python identifier, so this module was refused at inventory and never
        # staged. Reporting it keeps one bad row from denying the map for every other module.
        modules = [
            legacy(1, 'my-report.py', scripts=[(11, 'Report')]),
            legacy(2, 'deploy.py', scripts=[(12, 'Deploy')]),
        ]

        result = mapping.build_map(modules)

        self.assertEqual([entry['legacy_pk'] for entry in result['unmapped']], [1])
        self.assertIn('my-report.py', result['unmapped'][0]['path'])
        self.assertEqual([entry['legacy_pk'] for entry in result['modules']], [2])
        self.assertEqual([entry['class_name'] for entry in result['scripts']], ['Deploy'])

    def test_a_module_publishing_nothing_maps_but_contributes_no_script(self):
        modules = [legacy(1, 'helpers.py')]

        result = mapping.build_map(modules)

        self.assertEqual(len(result['modules']), 1)
        self.assertEqual(result['scripts'], [])

    def test_nothing_to_map_is_not_an_error(self):
        self.assertEqual(mapping.build_map([]), {'modules': [], 'scripts': [], 'unmapped': []})
        self.assertEqual(mapping.project_keys({'modules': []}), [])


class ResolveScriptsTestCase(TestCase):
    """Turning a derived identity into the row that carries it, once one exists."""

    def setUp(self):
        self.modules = [legacy(1, 'deploy.py', scripts=[(11, 'Deploy'), (12, 'Remove')])]
        self.mapping = mapping.build_map(self.modules)
        self.key = self.mapping['modules'][0]['project_key']

    def project(self):
        return ScriptProject.objects.create(
            name='migrated',
            key=self.key,
            source_type=ProjectSourceTypeChoices.UPLOAD,
        )

    def test_every_entry_is_unresolved_when_the_project_was_never_staged(self):
        resolved, unresolved = mapping.resolve_scripts(self.mapping)

        self.assertEqual(resolved, {})
        self.assertEqual([entry['legacy_pk'] for entry in unresolved], [11, 12])

    def test_an_entry_resolves_to_the_row_that_carries_its_identity(self):
        project = self.project()
        row = NetBoxScript.objects.create(project=project, module_path='deploy', class_name='Deploy')

        resolved, unresolved = mapping.resolve_scripts(self.mapping)

        self.assertEqual(resolved, {11: row})
        self.assertEqual([entry['legacy_pk'] for entry in unresolved], [12])

    def test_a_class_no_revision_publishes_is_reported(self):
        project = self.project()
        NetBoxScript.objects.create(project=project, module_path='deploy', class_name='Deploy')
        NetBoxScript.objects.create(project=project, module_path='other', class_name='Remove')

        resolved, unresolved = mapping.resolve_scripts(self.mapping)

        # The Remove row exists but under a different module, so the identity does not match.
        self.assertEqual(list(resolved), [11])
        self.assertEqual([entry['class_name'] for entry in unresolved], ['Remove'])

    def test_a_retired_row_still_resolves(self):
        # Documented: this answers which row an identity names, and the callers differ on whether a
        # retired one may be used.
        project = self.project()
        row = NetBoxScript.objects.create(project=project, module_path='deploy', class_name='Deploy', is_retired=True)

        resolved, _unresolved = mapping.resolve_scripts(self.mapping)

        self.assertEqual(resolved[11], row)

    def test_a_row_in_another_project_does_not_resolve(self):
        other = ScriptProject.objects.create(
            name='hand made',
            key='hand-made',
            source_type=ProjectSourceTypeChoices.UPLOAD,
        )
        NetBoxScript.objects.create(project=other, module_path='deploy', class_name='Deploy')

        resolved, unresolved = mapping.resolve_scripts(self.mapping)

        self.assertEqual(resolved, {})
        self.assertEqual(len(unresolved), 2)


class ReusedProjectMappingTestCase(TestCase):
    def test_the_map_names_the_existing_project_not_the_proposal_key(self):
        source = DataSource.objects.create(name='Mapped source', type='local', source_url='file:///tmp/scripts')
        project = ScriptProject.objects.create(
            name='Existing automation',
            key='custom-project-key',
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=source,
            data_path='automation',
        )
        modules = [
            legacy(1, 'deploy.py', data_source_id=source.pk, data_path='automation/deploy.py', scripts=[(11, 'Deploy')])
        ]
        result = mapping.build_map(modules, resolve_existing=True)
        self.assertEqual(result['modules'][0]['project_key'], project.key)
        self.assertEqual(result['scripts'][0]['project_key'], project.key)
