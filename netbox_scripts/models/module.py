from django.core.exceptions import ValidationError
from django.db import models, router
from django.utils.translation import gettext_lazy as _

from netbox.models import PrimaryModel

from ..choices import ModuleDiscoveryStatusChoices
from ..storage.exceptions import UnsafePathError
from ..storage.paths import case_insensitive_nodes, normalize_source_path
from ..utils import source_path_to_dotted_name


class CustomScriptModule(PrimaryModel):
    """
    One executable entrypoint within a Custom Script Project.

    A module names one Python file of the project's source tree that discovery imports
    and publishes Scripts from. Helper files need no module row, they stay importable by
    the entrypoints without being one. Enabled module declarations are snapshotted into
    each revision at staging time, so editing them changes future revisions and never
    what an existing revision was validated against. The discovery fields describe the
    most recent validation of the current declaration and are system-managed. A
    declaration is identified by its project and source path, both frozen after creation,
    so a file that moves is a new declaration rather than a repointed one carrying results
    from a path it no longer names.
    """

    project = models.ForeignKey(
        to='netbox_scripts.CustomScriptProject',
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
        """Canonicalize the source path, validate it declares an importable entrypoint, and freeze identity."""
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
                    errors['project'] = _('The project cannot be changed once the module has been created.')
                if 'source_path' not in errors and original['source_path'] != self.source_path:
                    errors['source_path'] = _('The source path cannot be changed once the module has been created.')

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Persist the module in canonical importable form, refusing any change to an identity field."""
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
                    errors['project'] = _('The project cannot be changed once the module has been created.')
                if original['source_path'] != self.source_path:
                    errors['source_path'] = _('The source path cannot be changed once the module has been created.')
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)

    def get_discovery_status_color(self):
        """Return the badge color configured for this module's discovery status."""
        return ModuleDiscoveryStatusChoices.colors.get(self.discovery_status)

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
                        'This path collides with module "{path}" of the same project, because "{mine}" and '
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
