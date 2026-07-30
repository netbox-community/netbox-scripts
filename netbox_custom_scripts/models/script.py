from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from core.choices import JobNotificationChoices
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin

from ..constants import (
    MAX_SCRIPT_CLASS_NAME_LENGTH,
    MAX_SCRIPT_DISPLAY_NAME_LENGTH,
    MAX_SCRIPT_MODULE_PATH_LENGTH,
)


class CustomScript(JobsMixin, PrimaryModel):
    """
    One Custom Script class published by a validated revision.

    A script is identified by its project and by the dotted module path and class name of
    the module that defines it, so a class re-exported by a second entrypoint publishes
    once, and a class that moves between files becomes a new identity. The publishing
    entrypoint is provenance recorded in the revision snapshot, not a relational parent,
    because a helper file can publish a class without being an entrypoint itself.

    Rows are derived from an activated revision rather than authored. Synchronization owns
    the display name, description, metadata, retirement, and last seen revision, while
    enabled belongs to the administrator and no synchronization touches it. A script the
    active revision stops publishing is retired rather than deleted, which preserves the
    primary key and with it the Job history the row has accumulated.
    """

    project = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProject',
        on_delete=models.CASCADE,
        related_name='scripts',
    )
    module_path = models.CharField(
        verbose_name=_('module path'),
        max_length=MAX_SCRIPT_MODULE_PATH_LENGTH,
        db_collation='natural_sort',
        help_text=_('Dotted path of the project module that defines the Script class.'),
    )
    class_name = models.CharField(
        verbose_name=_('class name'),
        max_length=MAX_SCRIPT_CLASS_NAME_LENGTH,
        db_collation='natural_sort',
    )
    display_name = models.CharField(
        verbose_name=_('display name'),
        max_length=MAX_SCRIPT_DISPLAY_NAME_LENGTH,
        editable=False,
    )
    # Overrides the abstract base field. The authoring API bounds Meta.description nowhere,
    # so the inherited 200-character field would have forced a lossy truncation.
    description = models.TextField(
        verbose_name=_('description'),
        blank=True,
        editable=False,
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True,
    )
    is_retired = models.BooleanField(
        verbose_name=_('retired'),
        default=False,
        editable=False,
        help_text=_('Set when the active revision no longer publishes this Custom Script.'),
    )
    last_seen_revision = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProjectRevision',
        verbose_name=_('last seen revision'),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+',
        editable=False,
    )
    metadata = models.JSONField(
        verbose_name=_('metadata'),
        default=dict,
        blank=True,
        editable=False,
        help_text=_('Execution defaults the most recent validation read from the class.'),
    )

    class Meta:
        app_label = 'netbox_custom_scripts'
        ordering = ('project', 'module_path', 'class_name')
        verbose_name = _('custom script')
        verbose_name_plural = _('custom scripts')
        # Running is its own action, not a form of changing the row. The codename has to carry
        # the model name so that get_permission_for_model() composes the same string and
        # restrict(user, 'run') resolves. NetBox registers it as an ObjectPermission checkbox.
        permissions = (('run_customscript', 'Can run a Custom Script'),)
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'module_path', 'class_name'),
                name='unique_project_module_class',
            ),
        ]

    def __str__(self):
        return self.display_name

    @property
    def full_name(self):
        """Dotted name of the class within its project."""
        return f'{self.module_path}.{self.class_name}'

    # The four execution defaults are read out of metadata rather than off separate columns,
    # because validation writes them as one JSON record. These accessors are what everything
    # else reads, so no caller repeats a dictionary key or a fallback.

    @property
    def commit_default(self):
        """Whether the run form's commit toggle starts on."""
        return bool(self.metadata.get('commit_default', True))

    @property
    def scheduling_enabled(self):
        """Whether the published class allows this Custom Script to be scheduled."""
        return bool(self.metadata.get('scheduling_enabled', True))

    @property
    def job_timeout(self):
        """The run timeout in seconds, or None to use the system default."""
        return self.metadata.get('job_timeout')

    @property
    def job_timeout_display(self):
        """The run timeout as a phrase, since no timeout is a real setting rather than a gap."""
        if self.job_timeout is None:
            return _('System default')
        return ngettext('%(count)d second', '%(count)d seconds', self.job_timeout) % {'count': self.job_timeout}

    @property
    def notifications_default(self):
        """Who is notified when a run of this Custom Script finishes."""
        return self.metadata.get('notifications_default') or JobNotificationChoices.NOTIFICATION_ALWAYS

    def get_notifications_default_display(self):
        """Label for the notification policy, following the accessor ChoiceAttr looks for."""
        return dict(JobNotificationChoices).get(self.notifications_default, self.notifications_default)

    @property
    def is_executable(self):
        """Whether every enabling condition for running this script currently holds."""
        # The active revision is checked in its own right rather than inferred from retirement.
        # Deactivation does retire every script in the same transaction, so the two always agree
        # today, but that is two code paths agreeing rather than a guarantee, and a run has to
        # resolve its class out of a revision that is actually being served.
        return (
            self.enabled
            and not self.is_retired
            and self.project.enabled
            and self.project.active_revision_id is not None
        )
