from django import forms
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.db import transaction
from django.utils.translation import gettext_lazy as _

from core.models import DataSource
from netbox.forms import PrimaryModelForm
from utilities.exceptions import AbortRequest
from utilities.forms import get_field_value
from utilities.forms.fields import DynamicModelChoiceField, SlugField
from utilities.forms.rendering import FieldSet
from utilities.forms.widgets import HTMXSelect, SplitMultiSelectWidget

from ...choices import ProjectSourceTypeChoices
from ...ingestion import check_upload_conflicts, ingest_upload, prepare_upload, uploaded_source_path
from ...jobs import ProjectScriptFileRefreshJob
from ...models import ScriptProject
from ...permissions import ACTIVATE_PERMISSION, SOURCE_REFUSAL, moved_source_fields
from ...storage import config

__all__ = (
    'ScriptProjectAddScriptForm',
    'ScriptProjectEditForm',
    'ScriptProjectScriptFilesForm',
    'ScriptProjectUploadForm',
)


class ScriptProjectEditForm(PrimaryModelForm):
    """Create and edit form for the Script Project model."""

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

    def clean(self):
        """Refuse a source-field move by a user holding change but not activate."""
        super().clean()
        cleaned_data = self.cleaned_data
        request = getattr(self.instance, '_request', None)
        if self.instance.pk and request and not request.user.has_perm(ACTIVATE_PERMISSION):
            for field in moved_source_fields(self.instance.pk, cleaned_data):
                # Not a disabled widget: restrict_form_fields would fail an unviewable source first.
                self.add_error(field, SOURCE_REFUSAL)
        return cleaned_data

    fieldsets = (
        FieldSet('name', 'key', 'description', 'enabled', 'tags', name=_('Project')),
        FieldSet('source_type', 'data_source', 'data_path', name=_('Source')),
        FieldSet('activation_policy', name=_('Activation')),
    )

    class Meta:
        model = ScriptProject
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


class ScriptProjectUploadForm(PrimaryModelForm):
    """
    Create a Script Project from one uploaded script.

    The form asks for what a user knows and nothing the plugin can work out for itself. The
    uploaded file's name becomes the source path, the script file is declared automatically, and
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
        model = ScriptProject
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
        _check_upload_size(upload)
        return upload

    def save(self, *args, **kwargs):
        """Create and authorize the Project and declaration, then ingest the source after commit."""
        project = super().save(*args, **kwargs)
        upload = self.cleaned_data['upload_file']
        filename, content = upload.name, upload.read()
        activate_once = self.cleaned_data.get('activate_this_revision', False)
        _prepare_form_upload(self, project, filename, confirm_replace=False)
        transaction.on_commit(
            lambda: ingest_upload(
                project, filename=filename, content=content, declare=False, activate_once=activate_once
            ),
            using=project._state.db,
        )
        return project


class ScriptProjectAddScriptForm(PrimaryModelForm):
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
        model = ScriptProject
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
        _check_upload_size(upload)
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
        _prepare_form_upload(self, project, filename, confirm_replace=self.cleaned_data['confirm_replace'])
        transaction.on_commit(
            lambda: ingest_upload(project, filename=filename, content=content, declare=False),
            using=project._state.db,
        )
        return self.instance


class ScriptProjectScriptFilesForm(PrimaryModelForm):
    """Select which of a project's source modules are its executable script files."""

    script_files = forms.MultipleChoiceField(
        required=False,
        label=_('Script Files'),
        help_text=_(
            'Source files whose Scripts this Project publishes. Helpers need no selection. '
            'Saving a change restages the source, and the Revision Files tab lists '
            'what the current revision holds.'
        ),
    )

    fieldsets = (FieldSet('script_files', name=_('Script Files')),)

    class Meta:
        model = ScriptProject
        fields = ()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # This form reconciles child declarations and never saves the project itself, so the
        # attribute fields the base form contributes would accept input and then be discarded.
        # They belong on the edit form, where saving them means something.
        for name in ('owner', 'owner_group', 'comments'):
            self.fields.pop(name, None)
        declared = {script_file.source_path: script_file for script_file in self.instance.script_files.all()}
        candidates = set(self.instance.script_file_candidates())
        # Only an uploaded project can have a path the served tree lacks and a stored revision
        # holds: for a Data Source the candidates come from the live directory.
        awaiting = (
            self.instance.paths_awaiting_activation()
            if self.instance.source_type == ProjectSourceTypeChoices.UPLOAD
            else set()
        )
        choices = self._grouped_choices(declared, candidates, awaiting)
        field = self.fields['script_files']
        # The field's copy is what accepts a submitted path and the widget's is what renders the
        # two panes. A MultiWidget forwards neither to the other, so both are set.
        field.choices = choices
        field.widget = SplitMultiSelectWidget(choices=choices)
        enabled = [path for path, script_file in declared.items() if script_file.enabled]
        # No declaration rather than none enabled, or a deselection would be undone.
        if not declared and len(candidates) == 1:
            enabled = sorted(candidates)
        self.initial['script_files'] = enabled

    def _grouped_choices(self, declared, candidates, awaiting):
        """Return the selectable paths as optgroups, one per directory they sit in."""
        groups = {}
        for path in self.instance.declarable_script_files():
            # rpartition rather than a path library: these paths are already canonical, and
            # validators.py avoids normpath because it resolves '..' segments.
            directory = path.rpartition('/')[0]
            label = self._label(path, declared.get(path), path in candidates, path in awaiting)
            groups.setdefault(directory, []).append((path, label))
        # The empty key sorts first, so the project root leads whatever its files are called.
        return [(directory or _('(root)'), groups[directory]) for directory in sorted(groups)]

    @staticmethod
    def _label(path, script_file, available, awaiting=False):
        """Return the option label: the file's own name, annotated with why it might matter."""
        # The directory is the group header, so repeating it here would push the annotation off
        # the end of a narrow pane.
        name = path.rpartition('/')[2]
        if not available:
            if awaiting:
                return _('{name} (not in the active revision yet)').format(name=name)
            return _('{name} (missing from the source)').format(name=name)
        if script_file is None:
            return name
        return _('{name} ({status})').format(name=name, status=script_file.get_discovery_status_display())

    def save(self, *args, **kwargs):
        """Reconcile the declarations onto the selection, apply it to the source, and return the project."""
        selection = self.cleaned_data['script_files']
        request = getattr(self.instance, '_request', None)
        changed = self.instance.select_script_files(selection, user=request.user if request else None)
        if changed:
            # A revision freezes the enabled declarations at staging time, so the selection has
            # no effect until something restages. That is storage work, which never happens in a
            # request, so it is a job. A selection that did not move would resolve to the
            # revision that already exists, so the comparison keeps an unchanged save out of the
            # Job list rather than relying on the job to find nothing to do.
            ProjectScriptFileRefreshJob.enqueue_refresh(self.instance)
        return self.instance


def _prepare_form_upload(form, project, filename, *, confirm_replace):
    """Authorize the declaration before registering any post-commit storage work."""
    request = getattr(form.instance, '_request', None)
    try:
        prepare_upload(
            project, filename=filename, confirm_replace=confirm_replace, user=request.user if request else None
        )
    except ValidationError as error:
        raise AbortRequest(' '.join(error.messages)) from error
    except ImproperlyConfigured as error:
        # Unsafe branch routing. ObjectEditView.post() catches only AbortRequest and
        # PermissionsViolation, so anything else leaves the operator a 500.
        raise AbortRequest(str(error)) from error


def _check_upload_size(upload):
    """Refuse an oversized upload before its content is read or its project is saved."""
    limit = config.get_storage_limits().max_file_size
    if upload.size > limit:
        raise ValidationError(
            _('The file is larger than the {limit} byte limit for one source file.').format(limit=limit)
        )
