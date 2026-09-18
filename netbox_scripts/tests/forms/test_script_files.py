from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from netbox_scripts.choices import FileDiscoveryStatusChoices, RevisionStatusChoices
from netbox_scripts.forms import ScriptProjectScriptFilesForm
from netbox_scripts.models import ScriptFile, ScriptProject, ScriptProjectRevision
from utilities.exceptions import AbortRequest


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
        # The selected pane posts under the subwidget's own name, the only key the widget reads.
        return ScriptProjectScriptFilesForm(data={'script_files_1': paths}, instance=self.project)

    @staticmethod
    def paths(form):
        """Return every selectable path, flattened out of the optgroups."""
        return [value for _group, members in form.fields['script_files'].choices for value, _label in members]

    @staticmethod
    def labels(form):
        """Return the option label per path, flattened out of the optgroups."""
        return {value: label for _group, members in form.fields['script_files'].choices for value, label in members}

    @staticmethod
    def groups(form):
        """Return the optgroup headers in render order."""
        return [str(group) for group, _members in form.fields['script_files'].choices]

    def single_candidate_project(self, key):
        """Return a project whose source holds exactly one importable module."""
        project = ScriptProject.objects.create(name=key.replace('-', ' '), key=key)
        ScriptProjectRevision.objects.create(
            project=project,
            digest='e' * 64,
            manifest=manifest('only.py', 'notes.md'),
            status=RevisionStatusChoices.MATERIALIZED,
        )
        return project

    def undeclarable_project(self, key, *paths):
        """Return a project whose source holds paths the declaration layer will refuse."""
        project = ScriptProject.objects.create(name=key.replace('-', ' '), key=key)
        ScriptProjectRevision.objects.create(
            project=project,
            digest='9' * 64,
            manifest=manifest(*paths),
            status=RevisionStatusChoices.MATERIALIZED,
        )
        return project

    def test_the_choices_are_the_projects_importable_modules(self):
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(self.paths(form), ['deploy.py', 'tools/audit.py', 'tools/helpers.py'])

    def test_the_options_are_grouped_by_directory(self):
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(self.groups(form), ['(root)', 'tools'])

    def test_an_option_carries_the_file_name_rather_than_the_whole_path(self):
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(str(self.labels(form)['tools/audit.py']), 'audit.py')

    def test_a_nested_directory_is_its_own_group(self):
        # Grouped by the full relative directory, so tools/deep does not merge into tools.
        project = ScriptProject.objects.create(name='Deep Selection', key='deep-selection')
        ScriptProjectRevision.objects.create(
            project=project,
            digest='d' * 64,
            manifest=manifest('tools/audit.py', 'tools/deep/x.py'),
            status=RevisionStatusChoices.MATERIALIZED,
        )

        self.assertEqual(self.groups(ScriptProjectScriptFilesForm(instance=project)), ['tools', 'tools/deep'])

    def test_a_lone_candidate_starts_selected(self):
        project = self.single_candidate_project('lone-candidate')

        form = ScriptProjectScriptFilesForm(instance=project)

        self.assertEqual(form.initial['script_files'], ['only.py'])

    def test_two_candidates_start_unselected(self):
        # Two is a real choice, so a default would be the form deciding it.
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(form.initial['script_files'], [])

    def test_a_deselected_lone_candidate_stays_deselected(self):
        # The guard is a declaration existing, not one being enabled: keying on enabled would
        # undo the operator's deselection every time the tab was reopened.
        project = self.single_candidate_project('deselected-candidate')
        ScriptFile.objects.create(project=project, source_path='only.py', enabled=False)

        form = ScriptProjectScriptFilesForm(instance=project)

        self.assertEqual(form.initial['script_files'], [])

    def test_rendering_the_default_declares_nothing(self):
        # A preselection is a default, and validation imports whatever is declared, so nothing
        # may be declared until the operator submits.
        project = self.single_candidate_project('nothing-declared')

        ScriptProjectScriptFilesForm(instance=project)

        self.assertFalse(ScriptFile.objects.filter(project=project).exists())

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

    def test_a_project_with_declarations_starts_at_the_enabled_ones(self):
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
        self.assertIn('removed.py', self.paths(form))
        submitted = self.form(['removed.py'])
        self.assertTrue(submitted.is_valid(), submitted.errors)

    def test_a_missing_path_is_labelled(self):
        ScriptFile.objects.create(project=self.project, source_path='removed.py')
        form = ScriptProjectScriptFilesForm(instance=self.project)
        self.assertEqual(str(self.labels(form)['removed.py']), 'removed.py (missing from the source)')

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

        labels = self.labels(ScriptProjectScriptFilesForm(instance=project))
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

    def test_a_path_that_cannot_name_a_module_is_labelled(self):
        project = self.undeclarable_project('hyphen-candidate', 'my-file.py', 'deploy.py')

        labels = self.labels(ScriptProjectScriptFilesForm(instance=project))

        self.assertEqual(str(labels['my-file.py']), 'my-file.py (cannot be a Script File)')
        self.assertEqual(str(labels['deploy.py']), 'deploy.py')

    def test_selecting_a_path_that_cannot_name_a_module_is_a_field_error(self):
        project = self.undeclarable_project('hyphen-selection', 'my-file.py')

        form = ScriptProjectScriptFilesForm(data={'script_files_1': ['my-file.py']}, instance=project)

        self.assertFalse(form.is_valid())
        self.assertIn('script_files', form.errors)
        self.assertIn('not a valid Python identifier', form.errors['script_files'][0])

    def test_a_reserved_keyword_path_is_refused_too(self):
        # isidentifier() passes "class", so the check has to be the model's own refusal.
        project = self.undeclarable_project('keyword-selection', 'class.py')

        form = ScriptProjectScriptFilesForm(data={'script_files_1': ['class.py']}, instance=project)

        self.assertFalse(form.is_valid())
        self.assertIn('reserved Python keyword', form.errors['script_files'][0])

    def test_a_lone_candidate_that_cannot_be_declared_does_not_start_selected(self):
        # Otherwise the tab opens with a selection an unmodified Save cannot accept.
        project = self.undeclarable_project('lone-undeclarable', 'my-file.py')

        form = ScriptProjectScriptFilesForm(instance=project)

        self.assertEqual(form.initial['script_files'], [])
        self.assertIn('my-file.py', self.paths(form))

    def test_two_selected_paths_with_one_module_name_abort_the_request(self):
        project = self.undeclarable_project('module-collision', 'deploy.py', 'deploy/__init__.py')
        form = ScriptProjectScriptFilesForm(
            data={'script_files_1': ['deploy.py', 'deploy/__init__.py']}, instance=project
        )
        self.assertTrue(form.is_valid(), form.errors)

        with self.assertRaises(AbortRequest) as caught:
            form.save()

        self.assertIn('imports as "deploy"', caught.exception.message)
