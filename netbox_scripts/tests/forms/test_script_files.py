from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.forms import ScriptProjectScriptFilesForm
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision


def manifest(*paths):
    return [{'path': path, 'size': 1, 'sha256': 'f' * 64} for path in sorted(paths)]


class ScriptFileSelectionTestCase(TestCase):
    """Selection reconciles onto enabled, so a deselected declaration keeps its history."""

    @classmethod
    def setUpTestData(cls):
        cls.project = ScriptProject.objects.create(name='Selection Project', key='selection-project')
        ScriptProjectRevision.objects.create(
            project=cls.project,
            digest='a' * 64,
            manifest=manifest('deploy.py', 'tools/audit.py', 'tools/helpers.py', 'notes.md'),
            status=RevisionStatusChoices.MATERIALIZED,
        )

    def form(self, paths):
        return ScriptProjectScriptFilesForm(data={'script_files': paths}, instance=self.project)

    def test_the_choices_are_the_projects_importable_modules(self):
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(
            [value for value, _label in form.fields['script_files'].choices],
            ['deploy.py', 'tools/audit.py', 'tools/helpers.py'],
        )

    def test_selecting_creates_enabled_declarations(self):
        form = self.form(['deploy.py', 'tools/audit.py'])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(
            sorted(self.project.script_files.filter(enabled=True).values_list('source_path', flat=True)),
            ['deploy.py', 'tools/audit.py'],
        )

    def test_deselecting_disables_without_deleting(self):
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        ScriptFile.objects.filter(pk=script_file.pk).update(discovery_status=FileDiscoveryStatusChoices.DISCOVERED)
        form = self.form([])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        script_file.refresh_from_db()
        self.assertFalse(script_file.enabled)
        self.assertEqual(script_file.discovery_status, FileDiscoveryStatusChoices.DISCOVERED)

    def test_reselecting_reuses_the_same_row(self):
        script_file = ScriptFile.objects.create(project=self.project, source_path='deploy.py', enabled=False)
        form = self.form(['deploy.py'])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(self.project.script_files.count(), 1)
        script_file.refresh_from_db()
        self.assertTrue(script_file.enabled)

    def test_the_initial_selection_is_the_enabled_declarations(self):
        ScriptFile.objects.create(project=self.project, source_path='deploy.py')
        ScriptFile.objects.create(project=self.project, source_path='tools/audit.py', enabled=False)
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(form.initial['script_files'], ['deploy.py'])

    def test_a_path_outside_the_source_is_refused(self):
        form = self.form(['nowhere.py'])
        self.assertFalse(form.is_valid())

    def test_a_declared_path_missing_from_the_source_stays_selectable(self):
        # Otherwise the form is unsubmittable until the operator drops the declaration.
        ScriptFile.objects.create(project=self.project, source_path='removed.py')
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertIn('removed.py', [value for value, _label in form.fields['script_files'].choices])
        submitted = self.form(['removed.py'])
        self.assertTrue(submitted.is_valid(), submitted.errors)

    def test_a_missing_path_is_labelled(self):
        ScriptFile.objects.create(project=self.project, source_path='removed.py')
        form = ScriptProjectScriptFilesForm(instance=self.project)
        labels = dict(form.fields['script_files'].choices)
        self.assertEqual(str(labels['removed.py']), 'removed.py (missing from the source)')

    def test_a_path_only_a_newer_revision_holds_is_labelled_not_yet_active(self):
        project = ScriptProject.objects.create(name='Staged Selection', key='staged-selection')
        active = ScriptProjectRevision.objects.create(
            project=project, digest='b' * 64, manifest=manifest('deploy.py'), status=RevisionStatusChoices.ACTIVE
        )
        newer = ScriptProjectRevision.objects.create(
            project=project,
            digest='c' * 64,
            manifest=manifest('deploy.py', 'added.py'),
            status=RevisionStatusChoices.VALID,
        )
        ScriptProjectRevision.objects.filter(pk=active.pk).update(created=timezone.now() - timedelta(hours=2))
        ScriptProjectRevision.objects.filter(pk=newer.pk).update(created=timezone.now() - timedelta(hours=1))
        ScriptProject.objects.filter(pk=project.pk).update(active_revision=active)
        project = ScriptProject.objects.get(pk=project.pk)
        ScriptFile.objects.create(project=project, source_path='added.py')
        ScriptFile.objects.create(project=project, source_path='gone.py')

        labels = dict(ScriptProjectScriptFilesForm(instance=project).fields['script_files'].choices)
        self.assertEqual(str(labels['added.py']), 'added.py (not in the active revision yet)')
        self.assertEqual(str(labels['gone.py']), 'gone.py (missing from the source)')

    def test_select_script_files_refuses_an_unknown_path_directly(self):
        with self.assertRaises(ValidationError):
            self.project.select_script_files(['nowhere.py'])

    def test_a_nested_script_file_is_accepted(self):
        form = self.form(['tools/audit.py'])
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        script_file = self.project.script_files.get(source_path='tools/audit.py')
        self.assertTrue(script_file.enabled)
