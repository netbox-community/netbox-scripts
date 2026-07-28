from django.core.exceptions import ValidationError
from django.db import models, router
from django.utils.translation import gettext_lazy as _

from netbox.models import PrimaryModel

from ..choices import ModuleDiscoveryStatusChoices
from ..storage.exceptions import UnsafePathError
from ..storage.paths import normalize_source_path
from ..utils import source_path_to_dotted_name


class CustomScriptModule(PrimaryModel):
    """
    One executable entrypoint within a Custom Script Project.

    A module names one Python file of the project's source tree that discovery imports
    and publishes Scripts from. Helper files need no module row, they stay importable by
    the entrypoints without being one. Enabled module declarations are snapshotted into
    each revision at staging time, so editing them changes future revisions and never
    what an existing revision was validated against. The discovery fields describe the
    most recent validation of the current declaration and are system-managed.
    """

    project = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProject',
        on_delete=models.CASCADE,
        related_name='modules',
    )
    source_path = models.CharField(
        verbose_name=_('source path'),
        max_length=1000,
        db_collation='natural_sort',
        help_text=_('POSIX-style relative path of the entrypoint Python file within the project source tree.'),
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True,
    )
    discovery_status = models.CharField(
        verbose_name=_('discovery status'),
        max_length=50,
        choices=ModuleDiscoveryStatusChoices,
        default=ModuleDiscoveryStatusChoices.PENDING,
        editable=False,
    )
    discovery_error = models.TextField(
        verbose_name=_('discovery error'),
        blank=True,
        editable=False,
    )
    last_discovered_revision = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProjectRevision',
        verbose_name=_('last discovered revision'),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+',
        editable=False,
    )

    class Meta:
        app_label = 'netbox_custom_scripts'
        ordering = ('project', 'source_path')
        verbose_name = _('custom script module')
        verbose_name_plural = _('custom script modules')
        constraints = [
            models.UniqueConstraint(
                fields=('project', 'source_path'),
                name='unique_project_source_path',
            ),
        ]

    def __str__(self):
        return f'{self.project}: {self.source_path}'

    def clean(self):
        """Canonicalize the source path and validate it declares an importable entrypoint."""
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
            conflict = self._case_folded_sibling()
            if conflict is not None:
                errors['source_path'] = _(
                    'This path collides with module "{path}" of the same project when letter case is ignored.'
                ).format(path=conflict.source_path)

        if self.last_discovered_revision_id and self.last_discovered_revision.project_id != self.project_id:
            errors['last_discovered_revision'] = _('The last discovered revision must belong to this project.')

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Persist the module with its source path in canonical form."""
        # Snapshot building reads rows straight from the ORM, so canonical form is an
        # at-rest invariant rather than a clean() nicety. QuerySet.update() bypasses this
        # and must supply canonical values itself.
        try:
            self.source_path = normalize_source_path(self.source_path)
        except UnsafePathError as error:
            raise ValidationError({'source_path': str(error)}) from error
        super().save(*args, **kwargs)

    def _read_alias(self):
        """Return the alias this instance's persisted state should be read from."""
        return self._state.db or router.db_for_read(type(self), instance=self)

    def _case_folded_sibling(self):
        """Return a sibling module whose path case-folds to this one's form, if any."""
        # Accepted paths must materialize on every supported host, and hosts such as APFS
        # treat "Utils.py" and "utils.py" as one file. The snapshot validator enforces the
        # same rule, this check just surfaces it where the declaration is made.
        folded = self.source_path.casefold()
        siblings = type(self).objects.using(self._read_alias()).filter(project=self.project_id).exclude(pk=self.pk)
        return next((other for other in siblings if other.source_path.casefold() == folded), None)
