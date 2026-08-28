import django_tables2 as tables
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _

from netbox.tables import BaseTable, PrimaryModelTable, columns
from utilities.validators import url_scheme_is_allowed

from ..models import CustomScript
from ..scripts.logging import LogLevelChoices


class CustomScriptTable(PrimaryModelTable):
    """Table for the Custom Script list view. Retirement is a default column, not a filter."""

    display_name = tables.Column(
        linkify=True,
    )
    project = tables.Column(
        linkify=True,
    )
    enabled = columns.BooleanColumn()
    is_retired = columns.BooleanColumn()
    last_seen_revision = tables.Column(linkify=True)
    tags = columns.TagColumn(
        url_name='plugins:netbox_custom_scripts:customscript_list',
    )
    # Drops the default 'delete' item, which reverses a route this model does not register.
    # ActionsColumn resolves a URL for every action the viewer holds the permission for, so
    # the default renders fine for a narrowly permissioned user and raises for a superuser.
    # Run is an extra button rather than a member of the set: the set is a fixed class-level
    # dict of edit, delete and changelog, so naming anything else raises KeyError.
    actions = columns.ActionsColumn(
        actions=('edit', 'changelog'),
        extra_buttons="{% include 'netbox_custom_scripts/inc/script_actions.html' %}",
    )

    class Meta(PrimaryModelTable.Meta):
        model = CustomScript
        fields = (
            'pk',
            'id',
            'display_name',
            'project',
            'module_path',
            'class_name',
            'enabled',
            'is_retired',
            'last_seen_revision',
            'description',
            'comments',
            'owner',
            'owner_group',
            'tags',
            'created',
            'last_updated',
        )
        default_columns = ('display_name', 'project', 'module_path', 'class_name', 'enabled', 'is_retired')


class CustomScriptLogTable(BaseTable):
    """
    The log one run recorded, read back out of the Job rather than out of a model.

    Rows are the plain dictionaries the authoring API's logging helpers build, so this table is
    fed a list and has no queryset behind it.
    """

    index = tables.Column(
        verbose_name=_('Line'),
    )
    time = columns.DateTimeColumn(
        verbose_name=_('Time'),
        timespec='seconds',
    )
    status = tables.Column(
        verbose_name=_('Level'),
    )
    object = tables.Column(
        verbose_name=_('Object'),
    )
    message = columns.MarkdownColumn(
        verbose_name=_('Message'),
    )

    class Meta(BaseTable.Meta):
        empty_text = _('This run recorded no log entries.')
        fields = ('index', 'time', 'status', 'object', 'message')
        default_columns = ('index', 'time', 'status', 'object', 'message')

    def render_status(self, value):
        """Render one level as its badge, using the colour and label its ChoiceSet already carries."""
        return format_html(
            '<span class="badge text-bg-{}">{}</span>',
            LogLevelChoices.colors.get(value, 'secondary'),
            dict(LogLevelChoices).get(value, value),
        )

    def render_object(self, value, record):
        """Link the logged object when the run recorded a URL a browser may follow."""
        url = record.get('url')
        # A script author can log any object, and get_absolute_url() is whatever that class returns.
        if not url or not url_scheme_is_allowed(url):
            return value
        return format_html('<a href="{}">{}</a>', url, value)
