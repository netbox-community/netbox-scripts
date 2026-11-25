from django.template.loader import render_to_string
from django.urls import reverse

from netbox.ui.actions import LinkAction

from ..models import ScriptProjectRevision


class ServedFileAction(LinkAction):
    """A link from a Script File into the revision its Project serves, shown only when the user can open the file."""

    def get_url(self, context):
        """Return the view's URL for this file in that revision, or None when it is absent or outside the grant."""
        revision, file_index = context['object'].served_position
        if file_index is None:
            return None
        if not ScriptProjectRevision.objects.restrict(context['request'].user, 'view').filter(pk=revision.pk).exists():
            return None
        return reverse(self.view_name, kwargs={'pk': revision.pk, 'file_index': file_index})

    def render(self, context):
        """Render the link, or nothing when the file cannot be opened."""
        action_context = self.get_context(context)
        if action_context['url'] is None:
            return ''
        return render_to_string(self.template_name, action_context)
