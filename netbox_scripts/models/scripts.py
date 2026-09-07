from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, router
from django.utils.translation import gettext_lazy as _
from django.utils.translation import ngettext

from core.choices import JobNotificationChoices
from netbox.models import PrimaryModel
from netbox.models.features import JobsMixin

from ..choices import FileDiscoveryStatusChoices
from ..constants import (
    MAX_SCRIPT_CLASS_NAME_LENGTH,
    MAX_SCRIPT_DISPLAY_NAME_LENGTH,
    MAX_SCRIPT_MODULE_PATH_LENGTH,
)
from ..storage.exceptions import UnsafePathError
from ..storage.paths import case_insensitive_nodes, normalize_source_path
from ..utils import source_path_to_dotted_name


class NetBoxScript(JobsMixin, PrimaryModel):
    """
    One Custom Script class published by a validated revision.

    A script is identified by its project and by the dotted module path and class name of
    the module that defines it, so a class re-exported by a second script file publishes
    once, and a class that moves between files becomes a new identity. The publishing
    script file is provenance recorded in the revision snapshot, not a relational parent,
    because a helper file can publish a class without being a script file itself.

    Rows are derived from an activated revision rather than authored. Synchronization owns
    the display name, description, metadata, retirement, and last seen revision, while
    enabled and the three execution overrides belong to the administrator and no
    synchronization touches them. A script the
    active revision stops publishing is retired rather than deleted, which preserves the
    primary key and with it the Job history the row has accumulated.
    """

    project = models.ForeignKey(
        to='netbox_scripts.ScriptProject',
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


class ScriptFile(PrimaryModel):
    """
    One declared executable file within a Script Project.

    A Script File names one Python file of the project's source tree that discovery imports
    and publishes Scripts from. Helper files need no Script File row, they stay importable by
    the script files without being one. Enabled script file declarations are snapshotted into
    each revision at staging time, so editing them changes future revisions and never
    what an existing revision was validated against. The discovery fields describe the
    most recent validation of the current declaration and are system-managed. A
    declaration is identified by its project and source path, both frozen after creation,
    so a file that moves is a new declaration rather than a repointed one carrying results
    from a path it no longer names.
    """

    project = models.ForeignKey(
        to='netbox_scripts.ScriptProject',
        on_delete=models.CASCADE,
        related_name='script_files',
    )
    source_path = models.CharField(
        verbose_name=_('source path'),
        max_length=1000,
        db_collation='natural_sort',
        help_text=_('POSIX-style relative path of the script file within the project source tree.'),
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True,
    )
    discovery_status = models.CharField(
        verbose_name=_('discovery status'),
        max_length=50,
        choices=FileDiscoveryStatusChoices,
        default=FileDiscoveryStatusChoices.PENDING,
        editable=False,
    )
    discovery_error = models.TextField(
        verbose_name=_('discovery error'),
        blank=True,
        editable=False,
    )
    last_discovered_revision = models.ForeignKey(
        to='netbox_scripts.ScriptProjectRevision',
        verbose_name=_('last discovered revision'),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+',
        editable=False,
    )

    class Meta:
        app_label = 'netbox_scripts'
        ordering = ('project', 'source_path')
        verbose_name = _('script file')
        verbose_name_plural = _('script files')
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'source_path'),
                name='unique_project_source_path',
            ),
        ]

    def __str__(self):
        return f'{self.project}: {self.source_path}'

    def clean(self):
        """Canonicalize the source path, validate it declares an importable script file, and freeze identity."""
        super().clean()
        errors = {}

        try:
            self.source_path = normalize_source_path(self.source_path)
        except UnsafePathError as error:
            errors['source_path'] = str(error)

        if 'source_path' not in errors:
            try:
                source_path_to_dotted_name(self.source_path)
            except ValidationError as error:
                errors['source_path'] = error

        if 'source_path' not in errors and self.project_id:
            conflict = self._sibling_path_conflict()
            if conflict is not None:
                errors['source_path'] = conflict

        if self.last_discovered_revision_id and self.last_discovered_revision.project_id != self.project_id:
            errors['last_discovered_revision'] = _('The last discovered revision must belong to this project.')

        # Compared after canonicalization, so a re-spelling of the stored path is not a change.
        if not self._state.adding:
            original = (
                type(self)
                .objects.using(self._read_alias())
                .filter(pk=self.pk)
                .values('project_id', 'source_path')
                .first()
            )
            if original:
                if original['project_id'] != self.project_id:
                    errors['project'] = _('The project cannot be changed once the script file has been created.')
                if 'source_path' not in errors and original['source_path'] != self.source_path:
                    errors['source_path'] = _(
                        'The source path cannot be changed once the script file has been created.'
                    )

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Persist the script file in canonical importable form, refusing any change to an identity field."""
        # Snapshot building reads rows straight from the ORM, so canonical form is an
        # at-rest invariant rather than a clean() nicety. QuerySet.update() bypasses this
        # and must supply canonical values itself.
        try:
            self.source_path = normalize_source_path(self.source_path)
        except UnsafePathError as error:
            raise ValidationError({'source_path': str(error)}) from error
        # Same reason: an unimportable path freezes into a snapshot activation can only reject.
        try:
            source_path_to_dotted_name(self.source_path)
        except ValidationError as error:
            raise ValidationError({'source_path': error}) from error
        # A collision is only introduced by a new row or a moved path, and the constraint
        # behind this one is case-sensitive, so it cannot catch the pair itself.
        update_fields = kwargs.get('update_fields')
        if (self._state.adding or update_fields is None or 'source_path' in update_fields) and self.project_id:
            conflict = self._sibling_path_conflict()
            if conflict is not None:
                raise ValidationError({'source_path': conflict})

        # clean() gives the identity fields friendly per-field errors on the form and REST
        # paths. This guard is the backstop for ORM writes that skip validation.
        if not self._state.adding:
            # The persisted row is read from the alias this save writes to. Reading it from
            # anywhere else compares the new value against a different database.
            using = kwargs.get('using') or self._state.db or router.db_for_write(type(self), instance=self)
            original = type(self).objects.using(using).filter(pk=self.pk).values('project_id', 'source_path').first()
            if original:
                errors = {}
                if original['project_id'] != self.project_id:
                    errors['project'] = _('The project cannot be changed once the script file has been created.')
                if original['source_path'] != self.source_path:
                    errors['source_path'] = _(
                        'The source path cannot be changed once the script file has been created.'
                    )
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)

    def get_discovery_status_color(self):
        """Return the badge color configured for this script file's discovery status."""
        return FileDiscoveryStatusChoices.colors.get(self.discovery_status)

    def _read_alias(self):
        """Return the alias this instance's persisted state should be read from."""
        return self._state.db or router.db_for_read(type(self), instance=self)

    def _sibling_path_conflict(self):
        """Return the error for a sibling declaration this path cannot coexist with, or None."""
        # Per node, because "Lib/deploy.py" against "lib/audit.py" collides in the directory.
        mine = case_insensitive_nodes(self.source_path)
        dotted = source_path_to_dotted_name(self.source_path)
        siblings = type(self).objects.using(self._read_alias()).filter(project=self.project_id).exclude(pk=self.pk)
        for other in siblings:
            for form, node in case_insensitive_nodes(other.source_path).items():
                if form in mine and mine[form] != node:
                    return _(
                        'This path collides with script file "{path}" of the same project, because "{mine}" and '
                        '"{theirs}" differ only in letter case.'
                    ).format(path=other.source_path, mine=mine[form], theirs=node)
            try:
                if source_path_to_dotted_name(other.source_path) == dotted:
                    return _(
                        'This path imports as "{name}", the same module name as "{path}" of the same project.'
                    ).format(name=dotted, path=other.source_path)
            except ValidationError:
                continue  # A sibling that cannot import claims no module name.
        return None
