"""Object-level action buttons for the Custom Scripts plugin."""

from django.utils.translation import gettext_lazy as _

from netbox.object_actions import ObjectAction

__all__ = (
    'ActivateRevision',
    'AddScript',
    'RunScript',
)


class ActivateRevision(ObjectAction):
    """
    Put a Custom Script Project's newest validated revision into service.

    Only rendered when there is something to activate, so a project already serving its newest
    revision shows no button. The change permission, because it moves the project's pointer.
    """

    name = 'activate'
    label = _('Activate')
    permissions_required = {'change'}
    url_kwargs = ['pk']
    template_name = 'netbox_custom_scripts/buttons/activate.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this project has a revision waiting to go live."""
        return {'candidate': obj.activatable_revision()}


class AddScript(ObjectAction):
    """
    Upload one more script into an existing Custom Script Project.

    The change permission, not add, because the target route is the project's own detail route
    and what it changes is the project's source. The view additionally requires the Module add
    permission, which an action's permission set cannot express, so a user holding only the
    project half is refused by the view rather than by a hidden button.
    """

    name = 'add_script'
    label = _('Add Script')
    permissions_required = {'change'}
    url_kwargs = ['pk']
    template_name = 'netbox_custom_scripts/buttons/add_script.html'


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
    template_name = 'netbox_custom_scripts/buttons/run.html'

    @classmethod
    def get_context(cls, context, obj):
        """Tell the template whether this script can be run right now."""
        return {'executable': obj.is_executable}
