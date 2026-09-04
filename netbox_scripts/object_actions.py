"""Object-level action buttons for the Custom Scripts plugin."""

from django.utils.translation import gettext_lazy as _

from netbox.object_actions import ObjectAction

from .choices import ProjectSourceTypeChoices

__all__ = (
    'ActivateRevision',
    'AddScript',
    'ReconcileSource',
    'RepairScripts',
    'RunScript',
)


class ActivateRevision(ObjectAction):
    """
    Put a Custom Script Project's newest validated revision into service.

    Only rendered when there is something to activate, so a project already serving its newest
    revision shows no button. Its own permission rather than change, because choosing what code a
    project runs is more privileged than renaming it.
    """

    name = 'activate'
    label = _('Activate')
    permissions_required = {'activate'}
    url_kwargs = ['pk']
    template_name = 'netbox_scripts/buttons/activate.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this project has a revision waiting to go live."""
        return {'candidate': obj.activatable_revision()}


class AddScript(ObjectAction):
    """
    Upload one more script into an existing Custom Script Project.

    Only rendered for a project whose source is uploaded, since a Data Source-backed project
    rebuilds its source from its directory and ingestion refuses an upload into one.

    The change permission, not add, because the target route is the project's own detail route
    and what it changes is the project's source. The view also requires the Module add
    permission, which permissions_required cannot express because it names another model, so the
    button checks it here and renders inert rather than refusing after the operator has clicked.
    """

    name = 'add_script'
    label = _('Add Script')
    permissions_required = {'change'}
    url_kwargs = ['pk']
    template_name = 'netbox_scripts/buttons/add_script.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this project takes uploads, and whether the user may declare one."""
        return {
            'uploadable': obj.source_type == ProjectSourceTypeChoices.UPLOAD,
            'may_declare': context['request'].user.has_perm('netbox_scripts.add_customscriptmodule'),
        }


class ReconcileSource(ObjectAction):
    """
    Rebuild a Custom Script Project's source from its Data Source directory now.

    Only rendered for a Data Source-backed project, since an uploaded one has no directory to
    reconcile against. Its own permission rather than change, because what it changes is what the
    project serves.
    """

    name = 'reconcile'
    label = _('Reconcile Source')
    permissions_required = {'reconcile'}
    url_kwargs = ['pk']
    template_name = 'netbox_scripts/buttons/reconcile.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this project has a directory to reconcile against."""
        return {'synchronized': obj.source_type == ProjectSourceTypeChoices.DATA_SOURCE}


class RepairScripts(ObjectAction):
    """
    Republish a Custom Script Project's rows from the revision it is already serving.

    A recovery action for rows that drifted from the snapshot they derive from. It takes the
    activate permission, because republishing rows into service is what that grants, and it
    renders inert rather than hidden for a project serving nothing, so an operator sees why.
    """

    name = 'repair'
    label = _('Repair Scripts')
    permissions_required = {'activate'}
    url_kwargs = ['pk']
    template_name = 'netbox_scripts/buttons/repair.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this project is serving a revision to republish from."""
        return {'serving': obj.active_revision_id is not None}


class RunScript(ObjectAction):
    """
    Run one Custom Script against the revision its project is serving.

    Its own permission rather than change, because running a script is not editing the row, and
    the two are granted to different people. The button is rendered inert rather than hidden
    when the script cannot run, so an operator sees why instead of finding nothing.
    """

    name = 'run'
    label = _('Run')
    permissions_required = {'run'}
    url_kwargs = ['pk']
    template_name = 'netbox_scripts/buttons/run.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this script can be run right now, and why not."""
        reason = obj.run_refusal_reason
        return {'executable': reason is None, 'refusal_reason': reason}
