from django import forms
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelForm
from utilities.forms import get_field_value
from utilities.forms.fields import DynamicModelChoiceField, SlugField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import HTMXSelect

from ...choices import ProjectSourceTypeChoices
from ...ingestion import check_upload_conflicts, current_source_tree, ingest_upload, uploaded_source_path
from ...jobs import ProjectEntrypointRefreshJob
from ...models import CustomScriptProject

__all__ = (
    'CustomScriptProjectAddScriptForm',
    'CustomScriptProjectEditForm',
    'CustomScriptProjectEntrypointsForm',
    'CustomScriptProjectUploadForm',
)


class CustomScriptProjectEditForm(PrimaryModelForm):
    """Create and edit form for the Custom Script Project model."""

    key = SlugField(
        label=_('Key'),
        max_length=100,
        help_text=_('Stable user-facing project key. Cannot be changed after creation.'),
    )
    data_source = DynamicModelChoiceField(
        queryset=DataSource.objects.all(),
        required=False,
        label=_('Data source'),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # key and source_type are frozen after creation (model clean() enforces it), so
        # the widgets are disabled to match. Swap key off SlugWidget as well, so no
        # regenerate button renders next to a value that can no longer change.
        if self.instance and self.instance.pk:
            self.fields['key'].disabled = True
            self.fields['key'].widget = forms.TextInput()
            self.fields['source_type'].disabled = True
        # data_source and data_path apply only to data source-backed projects. The
        # htmx-refreshed selection decides, and model clean() still enforces ownership.
        if get_field_value(self, 'source_type') != ProjectSourceTypeChoices.DATA_SOURCE:
            del self.fields['data_source']
            del self.fields['data_path']

    fieldsets = (
        FieldSet('name', 'key', 'description', 'enabled', 'tags', name=_('Project')),
        FieldSet('source_type', 'data_source', 'data_path', name=_('Source')),
        FieldSet('activation_policy', name=_('Activation')),
    )

    class Meta:
        model = CustomScriptProject
        fields = (
            'name',
            'key',
            'description',
            'source_type',
            'data_source',
            'data_path',
            'activation_policy',
            'enabled',
            'owner',
            'comments',
            'tags',
        )
        widgets = {
            'source_type': HTMXSelect(),
        }


class CustomScriptProjectUploadForm(PrimaryModelForm):
    """
    Create a Custom Script Project from one uploaded script.

    The form asks for what a user knows and nothing the plugin can work out for itself. The
    uploaded file's name becomes the source path, the entrypoint is declared automatically, and
    the checkbox decides whether a valid revision goes live without a second step. Source paths,
    module names, digests, and storage locations are never asked for.
    """

    key = SlugField(
        label=_('Key'),
        max_length=100,
        slug_source='name',
        help_text=_('Stable user-facing project key. Cannot be changed after creation.'),
    )
    upload_file = forms.FileField(
        label=_('Script'),
        help_text=_('A Python module to publish. Its file name becomes the path within the Project.'),
    )
    activate_this_revision = forms.BooleanField(
        required=False,
        initial=True,
        label=_('Activate this upload'),
        help_text=_('Activate this one upload as soon as it validates, even when the policy below is manual.'),
    )

    fieldsets = (
        FieldSet('name', 'key', 'description', name=_('Project')),
        FieldSet('upload_file', 'activate_this_revision', name=_('Script')),
        FieldSet('activation_policy', name=_('Activation policy')),
    )

    class Meta:
        model = CustomScriptProject
        fields = ('name', 'key', 'description', 'activation_policy')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Fixed for this form rather than asked for, and set before validation so the model's
        # own source-ownership check sees the value it will be saved with.
        self.instance.source_type = ProjectSourceTypeChoices.UPLOAD

    def clean_upload_file(self):
        """Refuse a name the path policy or the Python-source rule rejects, before anything is created."""
        upload = self.cleaned_data['upload_file']
        # Validated here so the message lands on the field the user can fix.
        uploaded_source_path(upload.name)
        return upload

    def save(self, *args, **kwargs):
        """Create the project, and declare, stage and enqueue the uploaded script once it commits."""
        project = super().save(*args, **kwargs)
        upload = self.cleaned_data['upload_file']
        filename, content = upload.name, upload.read()
        activate_once = self.cleaned_data.get('activate_this_revision', False)
        # The editing view wraps this call in a transaction, so staging waits for the commit. A
        # rollback would otherwise keep the bytes while discarding every row that names them.
        transaction.on_commit(
            lambda: ingest_upload(project, filename=filename, content=content, activate_once=activate_once)
        )
        return project


class CustomScriptProjectAddScriptForm(PrimaryModelForm):
    """
    Add one more script to a Project that already has source.

    A revision is a whole tree, so this stages everything the Project already holds plus the
    new file. Replacing a file the Project already has needs the tick, because the file name
    alone cannot say whether the user meant to.
    """

    upload_file = forms.FileField(
        label=_('Script'),
        help_text=_('A Python module to add. Its file name becomes the path within the Project.'),
    )
    confirm_replace = forms.BooleanField(
        required=False,
        label=_('Replace the existing file'),
        help_text=_('Required only when the Project already holds a file at this path.'),
    )

    fieldsets = (FieldSet('upload_file', 'confirm_replace', name=_('Script')),)

    class Meta:
        model = CustomScriptProject
        fields = ()

    def clean_upload_file(self):
        """Refuse a name the path policy or the Python-source rule rejects."""
        upload = self.cleaned_data['upload_file']
        uploaded_source_path(upload.name)
        return upload

    def clean(self):
        """Require confirmation for a path the Project already holds, and refuse a colliding one."""
        super().clean()
        # Refused here rather than left to ingestion, which raises out of save(), where the
        # editing view does not catch it.
        if self.instance.source_type != ProjectSourceTypeChoices.UPLOAD:
            raise ValidationError(
                _('The source of "{project}" is reconciled from its Data Source rather than uploaded.').format(
                    project=self.instance
                )
            )
        upload = self.cleaned_data.get('upload_file')
        if upload is None:
            return self.cleaned_data
        path = uploaded_source_path(upload.name)
        # The rule lives in ingestion so this form and the REST upload action cannot disagree
        # about what replacing a file costs. Surfaced on the field rather than raised, because
        # the editing view does not catch a ValidationError out of clean().
        try:
            check_upload_conflicts(self.instance, path, confirm_replace=self.cleaned_data.get('confirm_replace'))
        except ValidationError as error:
            self.add_error('upload_file', error.messages)
        return self.cleaned_data

    def save(self, *args, **kwargs):
        """Stage the existing tree plus the new file as one new revision, once the request commits."""
        project = self.instance
        upload = self.cleaned_data['upload_file']
        filename, content = upload.name, upload.read()
        # Deferred like the upload form's, so a rollback leaves no bytes behind. The tree read
        # waits with it, so a rejected upload never pulls a whole tree out of the store.
        transaction.on_commit(
            lambda: ingest_upload(
                project,
                filename=filename,
                content=content,
                base_files=current_source_tree(project),
            )
        )
        return self.instance


class EntrypointCheckboxSelect(forms.CheckboxSelectMultiple):
    """A multiple-checkbox widget carrying the Bootstrap markup the rest of the form uses."""

    template_name = 'netbox_scripts/widgets/entrypoint_checkboxes.html'


class CustomScriptProjectEntrypointsForm(PrimaryModelForm):
    """Select which of a project's source modules are its executable entrypoints."""

    entrypoints = forms.MultipleChoiceField(
        required=False,
        widget=EntrypointCheckboxSelect(),
        label=_('Entrypoints'),
        help_text=_('Source modules whose Custom Scripts this Project publishes. Helpers need no selection.'),
    )

    fieldsets = (FieldSet('entrypoints', name=_('Entrypoints')),)

    class Meta:
        model = CustomScriptProject
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This form reconciles child declarations and never saves the project itself, so the
        # attribute fields the base form contributes would accept input and then be discarded.
        # They belong on the edit form, where saving them means something.
        for name in ('owner', 'owner_group', 'comments'):
            self.fields.pop(name, None)
        declared = {module.source_path: module for module in self.instance.modules.all()}
        candidates = set(self.instance.entrypoint_candidates())
        # Only an uploaded project can have a path the served tree lacks and a stored revision
        # holds: for a Data Source the candidates come from the live directory.
        awaiting = (
            self.instance.paths_awaiting_activation()
            if self.instance.source_type == ProjectSourceTypeChoices.UPLOAD
            else set()
        )
        self.fields['entrypoints'].choices = [
            (path, self._label(path, declared.get(path), path in candidates, path in awaiting))
            for path in self.instance.declarable_entrypoints()
        ]
        self.initial['entrypoints'] = [path for path, module in declared.items() if module.enabled]

    @staticmethod
    def _label(path, module, available, awaiting=False):
        """Return the checkbox label, annotated with why an operator might care about the path."""
        if not available:
            if awaiting:
                return _('{path} (not in the active revision yet)').format(path=path)
            return _('{path} (missing from the source)').format(path=path)
        if module is None:
            return path
        return _('{path} ({status})').format(path=path, status=module.get_discovery_status_display())

    def save(self, *args, **kwargs):
        """Reconcile the declarations onto the selection, apply it to the source, and return the project."""
        selection = self.cleaned_data['entrypoints']
        changed = set(selection) != set(self.initial.get('entrypoints') or ())
        self.instance.select_entrypoints(selection)
        if changed:
            # A revision freezes the enabled declarations at staging time, so the selection has
            # no effect until something restages. That is storage work, which never happens in a
            # request, so it is a job. A selection that did not move would resolve to the
            # revision that already exists, so the comparison keeps an unchanged save out of the
            # Job list rather than relying on the job to find nothing to do.
            ProjectEntrypointRefreshJob.enqueue_refresh(self.instance)
        return self.instance
