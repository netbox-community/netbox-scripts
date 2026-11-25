import logging

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.decorators import method_decorator
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext
from django.views.decorators.cache import never_cache

from netbox.ui import layout
from netbox.ui.breadcrumbs import Breadcrumb, object_view_url
from netbox.ui.panels import ContextTablePanel
from netbox.views import generic
from utilities.permissions import get_permission_for_model
from utilities.views import ViewTab, register_model_view

from .. import activation
from ..constants import ACTIVATABLE_REVISION_STATUSES, MAX_SOURCE_DISPLAY_SIZE
from ..models import ScriptFile, ScriptProject, ScriptProjectRevision
from ..storage import config, store
from ..storage.exceptions import ActivationError, RevisionCorruptError, StorageError
from ..storage.manifest import validate_manifest
from ..storage.paths import revision_key
from ..tables import ScriptProjectRevisionFileTable, ScriptProjectRevisionProblemTable
from ..ui import (
    ScriptProjectRevisionDiscoveryPanel,
    ScriptProjectRevisionFilePanel,
    ScriptProjectRevisionPanel,
    ScriptProjectRevisionSourcePanel,
    ScriptProjectRevisionStatePanel,
)

logger = logging.getLogger('netbox.plugins.netbox_scripts.views')


def activation_message(revision, scripts):
    """Report an activation, naming what the revision publishes and what it retired."""
    if not scripts.published:
        # A revision can validate and publish nothing, which is the state an operator is most
        # likely to misread as success.
        return _('Revision {revision} is now the active revision. It publishes no Scripts.').format(
            revision=revision.short_digest
        )
    published = ngettext(
        'Revision {revision} is now the active revision, publishing {count} Script.',
        'Revision {revision} is now the active revision, publishing {count} Scripts.',
        scripts.published,
    ).format(revision=revision.short_digest, count=scripts.published)
    if not scripts.retired:
        return published
    # A revision that drops a script retires its row, which is the outcome least likely to be
    # expected and was previously counted as though it had been published.
    retired = ngettext(
        'It retired {count} Script the previous revision published.',
        'It retired {count} Scripts the previous revision published.',
        scripts.retired,
    ).format(count=scripts.retired)
    return f'{published} {retired}'


@register_model_view(ScriptProjectRevision)
class ScriptProjectRevisionView(generic.ObjectView):
    """Detail view for a single revision, reached from the project's Revisions tab."""

    # No add, edit or delete route on this model. The Revisions tab carries the two buttons.
    actions = ()
    queryset = ScriptProjectRevision.objects.select_related('project')
    template_name = 'generic/object.html'
    layout = layout.SimpleLayout(
        left_panels=[ScriptProjectRevisionPanel()],
        right_panels=[ScriptProjectRevisionStatePanel()],
        bottom_panels=[
            ContextTablePanel('problems_table', title=_('Recorded problems')),
        ],
        # Revisions have no list view, so the trail starts at Script Projects instead of an unlinked root crumb.
        root_breadcrumb=False,
        breadcrumbs=[
            Breadcrumb(label=_('Script Projects'), url=reverse_lazy('plugins:netbox_scripts:scriptproject_list')),
            Breadcrumb('project'),
            Breadcrumb(
                'project', label=_('Revisions'), url=object_view_url('plugins:netbox_scripts:scriptproject_revisions')
            ),
        ],
    )

    def get_extra_context(self, request, instance):
        """Supply the problem table, withholding its key when there are none."""
        context = {}
        # ContextTablePanel renders nothing for an unresolved key, which is how a revision with
        # no problems avoids an empty card.
        if problems := instance.problems:
            problems_table = ScriptProjectRevisionProblemTable(problems, orderable=False)
            problems_table.configure(request)
            context['problems_table'] = problems_table
        return context


@register_model_view(ScriptProjectRevision, 'files', path='files')
class ScriptProjectRevisionFilesView(generic.ObjectChildrenView):
    """The files one revision holds, marked by the Script Files it froze rather than the live declarations."""

    queryset = ScriptProjectRevision.objects.select_related('project')
    table = ScriptProjectRevisionFileTable
    actions = ()
    tab = ViewTab(
        label=_('Files'),
        badge=lambda obj: obj.file_count,
        weight=500,
    )

    def get_children(self, request, parent):
        """Return a numbered row per manifest entry, plus one per frozen Script File the manifest lacks."""
        frozen = {entry['source_path'] for entry in parent.script_file_snapshot}
        present = {entry['path'] for entry in parent.manifest}
        revision_id = parent.pk if parent.has_stored_source else None
        rows = [
            {**entry, 'script_file': entry['path'] in frozen, 'revision_id': revision_id, 'file_index': index}
            for index, entry in enumerate(parent.manifest)
        ]
        rows += [
            {'path': path, 'size': None, 'sha256': None, 'script_file': True, 'missing': True}
            for path in frozen - present
        ]
        return sorted(rows, key=lambda row: row['path'])


def revision_file_index(revision, file_index):
    """Return file_index if the revision holds stored source and the index is in its manifest, else raise Http404."""
    if not revision.has_stored_source or file_index >= len(revision.manifest):
        raise Http404
    return file_index


@register_model_view(ScriptProjectRevision, 'file', path='files/<int:file_index>')
class ScriptProjectRevisionFileView(generic.ObjectView):
    """The File tab of one revision file: its manifest entry, Project and Script File."""

    actions = ()
    queryset = ScriptProjectRevision.objects.select_related('project')
    template_name = 'netbox_scripts/scriptprojectrevision_file.html'
    layout = layout.SimpleLayout(
        left_panels=[ScriptProjectRevisionFilePanel(accessor='source_file')],
        right_panels=[ScriptProjectRevisionDiscoveryPanel(accessor='discovered_file')],
    )

    def get_object(self, **kwargs):
        """Return the revision the URL names. The file index selects within it."""
        return super().get_object(pk=kwargs['pk'])

    def get_extra_context(self, request, instance):
        """Supply the indexed entry with its Project and Script File, and that file's discovery of this revision."""
        file_index = revision_file_index(instance, self.kwargs['file_index'])
        entry = instance.manifest[file_index]
        script_file = (
            ScriptFile.objects.restrict(request.user, 'view')
            .filter(project=instance.project, source_path=entry['path'])
            .first()
        )
        discovered = script_file if script_file and script_file.last_discovered_revision_id == instance.pk else None
        return {
            'source_file': {**entry, 'project': instance.project, 'script_file': script_file},
            'discovered_file': discovered,
            'file_index': file_index,
            'file_tab': 'file',
        }


@register_model_view(ScriptProjectRevision, 'source', path='files/<int:file_index>/source')
@method_decorator(never_cache, name='dispatch')
class ScriptProjectRevisionSourceView(generic.ObjectView):
    """The Source tab of one revision file: its text, read through the verified store."""

    actions = ()
    queryset = ScriptProjectRevision.objects.select_related('project')
    template_name = 'netbox_scripts/scriptprojectrevision_file.html'
    layout = layout.Layout(
        layout.Row(
            layout.Column(
                ScriptProjectRevisionSourcePanel(accessor='source_file'),
            ),
        ),
    )

    def get_object(self, **kwargs):
        """Return the revision the URL names. The file index selects within it."""
        return super().get_object(pk=kwargs['pk'])

    def get_extra_context(self, request, instance):
        """Supply the indexed manifest entry with its text, or why the text is withheld."""
        try:
            validate_manifest(instance.manifest, instance.digest)
        except RevisionCorruptError as error:
            return {
                'source_file': {'text': None, 'unavailable': str(error)},
                'file_index': self.kwargs['file_index'],
                'file_tab': 'source',
            }
        file_index = revision_file_index(instance, self.kwargs['file_index'])
        entry = instance.manifest[file_index]
        text = unavailable = None
        try:
            if entry['size'] > MAX_SOURCE_DISPLAY_SIZE:
                unavailable = _('"{path}" is {size} bytes, over the {limit} byte limit for displaying source.').format(
                    path=entry['path'], size=entry['size'], limit=MAX_SOURCE_DISPLAY_SIZE
                )
            else:
                key = revision_key(instance.project.storage_key, instance.digest, entry['path'])
                data = store.read_verified(config.get_storage(), key, entry)
                # UTF-8 decodes NUL, which binary files and BOM-less UTF-16 hold and source text does not.
                if b'\x00' in data:
                    unavailable = _('This file is binary or not UTF-8 encoded, so it cannot be displayed.')
                else:
                    text = data.decode('utf-8-sig')
        except RevisionCorruptError as error:
            unavailable = str(error)
        except StorageError:
            logger.exception('Could not read "%s" from revision %s.', entry['path'], instance.pk)
            unavailable = _('The stored file could not be read.')
        except UnicodeDecodeError:
            unavailable = _('This file is binary or not UTF-8 encoded, so it cannot be displayed.')
        return {
            'source_file': {**entry, 'text': text, 'unavailable': unavailable},
            'file_index': file_index,
            'file_tab': 'source',
        }


class RevisionServiceView(generic.ObjectView):
    """
    Shared plumbing for the two actions that move a project between revisions.

    GET confirms and POST performs, the shape every other state change here takes. The Revisions
    tab reaches them as ordinary links rather than posting directly, because the children view
    wraps its table in a form for bulk actions and a nested form is invalid HTML that browsers
    discard, which would submit the outer form to the tab URL instead.

    Permission is the owning project's activate permission, because what these change is what the
    project serves, rather than the revision view permission the inherited check would want.
    Reaching them still needs that view permission, since the Revisions tab is the only route to
    them. Object-level project permissions apply through the project queryset the revisions are
    filtered against.
    """

    # ObjectView's default names three routes this model does not register.
    actions = ()
    # return_url() reaches the project after an operation that may have found it deleted, so a
    # lazy fetch here would raise past the handler that just caught it.
    queryset = ScriptProjectRevision.objects.select_related('project')

    def get_required_permission(self):
        """Require the owning project's activate permission, not the revision's own."""
        return get_permission_for_model(ScriptProject, 'activate')

    def has_permission(self):
        """Gate on the project permission, and narrow the revisions to permitted projects."""
        user = self.request.user
        if not user.has_perm(self.get_required_permission()):
            return False
        self.queryset = self.queryset.filter(project__in=ScriptProject.objects.restrict(user, 'activate'))
        return True

    @staticmethod
    def return_url(revision):
        """Return the project's Revisions tab, which is where both buttons are rendered."""
        return f'{revision.project.get_absolute_url()}revisions/'

    def get(self, request, **kwargs):
        """Render the confirmation, which posts back to this same route."""
        revision = self.get_object(**kwargs)
        return render(
            request,
            self.template_name,
            {
                'object': revision,
                'project': revision.project,
                'return_url': self.return_url(revision),
            },
        )


@register_model_view(ScriptProjectRevision, 'activate', path='activate')
class ScriptProjectRevisionActivateView(RevisionServiceView):
    """
    Put one specific revision of a project into service.

    The queryset excludes a revision whose status does not allow it, the active one included, so
    the route refuses what the tab declines to offer. Withholding the button is not enough: an
    action filters by permission and never by route, and reaching this view for the revision
    already in force would silently take the repair path while the confirmation page promised a
    retirement that cannot happen. Repair Scripts on the Project is the route for that.
    """

    queryset = ScriptProjectRevision.objects.select_related('project').filter(status__in=ACTIVATABLE_REVISION_STATUSES)
    template_name = 'netbox_scripts/scriptprojectrevision_activate.html'

    def post(self, request, **kwargs):
        """Activate the revision, reporting a refusal rather than raising at the user."""
        revision = self.get_object(**kwargs)
        try:
            result = activation.activate_revision(revision)
        except (ActivationError, RevisionCorruptError) as error:
            # Expected refusals: the revision moved on, or its stored tree no longer matches.
            messages.error(request, _('The revision could not be activated: {error}').format(error=error))
        except (StorageError, OSError):
            logger.exception('Could not activate revision %s of Script Project %s.', revision.pk, revision.project_id)
            messages.error(request, _('The revision could not be activated.'))
        else:
            messages.success(request, activation_message(revision, result.scripts))
        return redirect(self.return_url(revision))


@register_model_view(ScriptProjectRevision, 'deactivate', path='deactivate')
class ScriptProjectRevisionDeactivateView(RevisionServiceView):
    """Stand a project down from the revision it is serving."""

    template_name = 'netbox_scripts/scriptprojectrevision_deactivate.html'

    def post(self, request, **kwargs):
        """Retire the revision and clear the project's pointer, reporting a refusal."""
        revision = self.get_object(**kwargs)
        try:
            activation.deactivate_revision(revision)
        except ActivationError as error:
            messages.error(request, _('The revision could not be deactivated: {error}').format(error=error))
        except StorageError:
            logger.exception('Could not deactivate revision %s of Script Project %s.', revision.pk, revision.project_id)
            messages.error(request, _('The revision could not be deactivated.'))
        else:
            messages.success(
                request,
                _('Revision {revision} is retired and the Project is serving nothing.').format(
                    revision=revision.short_digest
                ),
            )
        return redirect(self.return_url(revision))
