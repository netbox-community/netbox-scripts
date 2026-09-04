from django.core.validators import MinValueValidator
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
    enabled and the three execution overrides belong to the administrator and no
    synchronization touches them. A script the
    active revision stops publishing is retired rather than deleted, which preserves the
    primary key and with it the Job history the row has accumulated.
    """

    project = models.ForeignKey(
        to='netbox_scripts.CustomScriptProject',
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
    commit_default_override = models.BooleanField(
        verbose_name=_('commit default override'),
        blank=True,
        null=True,
        help_text=_('Overrides the class commit default. Leave unset to follow the class.'),
    )
    job_timeout_override = models.PositiveIntegerField(
        verbose_name=_('job timeout override'),
        blank=True,
        null=True,
        # A zero-second timeout is not a setting. rq cancels the alarm outright for it, so it
        # would read as "no timeout" while the row says otherwise.
        validators=[MinValueValidator(1)],
        help_text=_('Overrides the class run timeout, in seconds. Leave unset to follow the class.'),
    )
    notifications_default_override = models.CharField(
        verbose_name=_('notifications default override'),
        max_length=30,
        choices=JobNotificationChoices,
        blank=True,
        help_text=_('Overrides the class notification policy. Leave unset to follow the class.'),
    )
    is_retired = models.BooleanField(
        verbose_name=_('retired'),
        default=False,
        editable=False,
        help_text=_('Set when the active revision no longer publishes this Custom Script.'),
    )
    last_seen_revision = models.ForeignKey(
        to='netbox_scripts.ScriptProjectRevision',
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
        app_label = 'netbox_scripts'
        ordering = ('project', 'module_path', 'class_name')
        verbose_name = _('custom script')
        verbose_name_plural = _('custom scripts')
        # Their own actions, not forms of changing the row. The codename is the bare action,
        # because the permission picker offers it verbatim and the backend composes
        # f'{app}.{action}_{model}' from what an administrator ticked.
        permissions = (
            ('run', 'Can run a Custom Script'),
            ('schedule', 'Can schedule a Custom Script'),
        )
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

    # Validation writes the four class defaults as one JSON record, which every activation
    # replaces, so an operator's override lives in a column of its own. These accessors are the
    # one place the precedence is resolved: the override, then the class, then the built-in.

    @property
    def commit_default(self):
        """Whether the run form's commit toggle starts on, an override winning over the class."""
        if self.commit_default_override is not None:
            return self.commit_default_override
        return bool(self.metadata.get('commit_default', True))

    @property
    def scheduling_enabled(self):
        """Whether the published class allows this Custom Script to be scheduled."""
        return bool(self.metadata.get('scheduling_enabled', True))

    @property
    def job_timeout(self):
        """The run timeout in seconds, or None to use the system default."""
        # Empty is spent on inherit, so overriding a declared timeout back to the system default
        # is not expressible. An operator sets the number they want instead.
        if self.job_timeout_override is not None:
            return self.job_timeout_override
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
        return (
            self.notifications_default_override
            or self.metadata.get('notifications_default')
            or JobNotificationChoices.NOTIFICATION_ALWAYS
        )

    def get_notifications_default_display(self):
        """Label for the notification policy, following the accessor ChoiceAttr looks for."""
        return dict(JobNotificationChoices).get(self.notifications_default, self.notifications_default)

    @property
    def is_executable(self):
        """Whether every enabling condition for running this script currently holds."""
        return self.run_refusal_reason is None

    @property
    def run_refusal_reason(self):
        """The first unmet condition for running this script, phrased for an operator, or None."""
        if not self.enabled:
            return _('It is disabled.')
        if self.is_retired:
            return _('It is retired, so its Project no longer publishes it.')
        if not self.project.enabled:
            return _('Its Project is disabled.')
        # Checked in its own right rather than inferred from retirement: deactivation retires
        # every script in the same transaction, but that is two code paths agreeing, not a guarantee.
        if self.project.active_revision_id is None:
            return _('Its Project is serving no revision.')
        return None
