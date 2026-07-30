from django.db import models
from django.utils.translation import gettext_lazy as _

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

    @property
    def is_executable(self):
        """Whether every enabling condition for running this script currently holds."""
        return self.enabled and not self.is_retired and self.project.enabled
