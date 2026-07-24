import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.models import ChangeLoggedModel, PrimaryModel

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from ..validators import data_paths_overlap, normalize_data_path


class CustomScriptProject(PrimaryModel):
    """
    One Custom Script Project: a single script source tree and one future Python package.

    A project owns either uploaded content or a directory of a data source, never both.
    The key and source type are frozen after creation, and the storage key never changes.
    """

    name = models.CharField(
        verbose_name=_('name'),
        max_length=100,
        db_collation='natural_sort',
    )
    key = models.SlugField(
        verbose_name=_('key'),
        max_length=100,
        unique=True,
        help_text=_('Stable user-facing project key. Cannot be changed after creation.'),
    )
    storage_key = models.UUIDField(
        default=uuid.uuid4,
        unique=True,
        editable=False,
        help_text=_('Immutable storage and runtime identity.'),
    )
    source_type = models.CharField(
        verbose_name=_('source type'),
        max_length=50,
        choices=ProjectSourceTypeChoices,
        default=ProjectSourceTypeChoices.UPLOAD,
    )
    data_source = models.ForeignKey(
        to='core.DataSource',
        on_delete=models.PROTECT,
        blank=True,
        null=True,
        related_name='+',
        help_text=_('Remote data source backing this project.'),
    )
    data_path = models.CharField(
        verbose_name=_('data path'),
        max_length=1000,
        blank=True,
        help_text=_('POSIX-style relative directory path to the project root within the data source.'),
    )
    activation_policy = models.CharField(
        verbose_name=_('activation policy'),
        max_length=50,
        choices=ActivationPolicyChoices,
        default=ActivationPolicyChoices.MANUAL,
        help_text=_(
            'Whether new revisions of this project activate automatically when valid, or require manual activation.'
        ),
    )
    enabled = models.BooleanField(
        verbose_name=_('enabled'),
        default=True,
    )

    class Meta:
        app_label = 'netbox_custom_scripts'
        ordering = ('name',)
        verbose_name = _('custom script project')
        verbose_name_plural = _('custom script projects')
        constraints = [
            models.CheckConstraint(
                name='enforce_source_ownership',
                condition=(
                    Q(
                        source_type=ProjectSourceTypeChoices.UPLOAD,
                        data_source__isnull=True,
                        data_path='',
                    )
                    | (
                        Q(
                            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                            data_source__isnull=False,
                        )
                        & ~Q(data_path='')
                    )
                ),
            ),
            models.UniqueConstraint(
                fields=('data_source', 'data_path'),
                condition=Q(source_type=ProjectSourceTypeChoices.DATA_SOURCE),
                name='unique_data_source_path',
            ),
        ]

    def __str__(self):
        return self.name

    def clean(self):
        """Validate source ownership, canonicalize the data path, and freeze identity fields."""
        super().clean()
        errors = {}

        try:
            self.data_path = normalize_data_path(self.data_path)
        except ValidationError as error:
            errors['data_path'] = error

        if self.source_type == ProjectSourceTypeChoices.DATA_SOURCE:
            if not self.data_source:
                errors['data_source'] = _('A data source is required for data source-backed projects.')
            if not self.data_path and 'data_path' not in errors:
                errors['data_path'] = _(
                    'A directory path within the data source is required. The repository root is not allowed.'
                )
        else:
            if self.data_source:
                errors['data_source'] = _('A data source applies only to data source-backed projects.')
            if self.data_path:
                errors['data_path'] = _('A data path applies only to data source-backed projects.')

        if (
            self.source_type == ProjectSourceTypeChoices.DATA_SOURCE
            and self.data_source_id
            and self.data_path
            and 'data_path' not in errors
        ):
            conflict = self._overlapping_sibling()
            if conflict is not None:
                errors['data_path'] = _(
                    'This data path overlaps with project "%(name)s" (%(path)s) on the same data source.'
                ) % {'name': conflict.name, 'path': conflict.data_path}

        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values('key', 'source_type').first()
            if original:
                if original['key'] != self.key:
                    errors['key'] = _('The project key cannot be changed once the project has been created.')
                if original['source_type'] != self.source_type:
                    errors['source_type'] = _('The source type cannot be changed once the project has been created.')

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Persist the project, refusing any change to an immutable identity field."""
        # clean() gives key and source_type friendly per-field errors on the form and
        # REST paths. This guard is the backstop for ORM writes that skip validation.
        # storage_key is on no form or serializer (editable=False), so it is guarded
        # here only.
        if not self._state.adding:
            original = type(self).objects.filter(pk=self.pk).values('key', 'source_type', 'storage_key').first()
            if original:
                errors = {}
                if original['key'] != self.key:
                    errors['key'] = _('The project key cannot be changed once the project has been created.')
                if original['source_type'] != self.source_type:
                    errors['source_type'] = _('The source type cannot be changed once the project has been created.')
                if original['storage_key'] != self.storage_key:
                    errors['storage_key'] = _('The storage key is immutable.')
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)

    def _overlapping_sibling(self):
        siblings = (
            type(self)
            .objects.filter(
                source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                data_source=self.data_source,
            )
            .exclude(pk=self.pk)
        )
        for other in siblings:
            if data_paths_overlap(self.data_path, other.data_path):
                return other
        return None


class CustomScriptProjectRevision(ChangeLoggedModel):
    """
    One immutable snapshot of a Custom Script Project's complete source tree.

    A revision records what was staged, not how it is served. Its content fields are
    frozen once the row exists, so a job can be replayed against exactly the tree it
    ran on. Only a valid revision carries a content digest, which addresses the tree
    and deduplicates repeated uploads of identical content. An invalid staging attempt
    is kept with a null digest so its errors and partial manifest stay inspectable,
    and so two different broken trees that happen to share an accepted subset cannot
    collide on one digest.
    """

    project = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProject',
        on_delete=models.CASCADE,
        related_name='revisions',
    )
    digest = models.CharField(
        verbose_name=_('digest'),
        max_length=64,
        blank=True,
        null=True,
        validators=[
            RegexValidator(
                regex=r'^[0-9a-f]{64}$',
                message=_('The digest must be 64 lowercase hexadecimal characters.'),
            )
        ],
        help_text=_('Content address of the source tree. Set only once the revision is known to be valid.'),
    )
    status = models.CharField(
        verbose_name=_('status'),
        max_length=50,
        choices=RevisionStatusChoices,
        default=RevisionStatusChoices.STAGING,
    )
    manifest = models.JSONField(
        verbose_name=_('manifest'),
        default=list,
        blank=True,
        help_text=_('Sorted list of accepted source files, each with its path, size, and checksum.'),
    )
    file_count = models.PositiveIntegerField(
        verbose_name=_('file count'),
        default=0,
    )
    total_size = models.PositiveBigIntegerField(
        verbose_name=_('total size'),
        default=0,
        help_text=_('Combined size in bytes of every accepted source file.'),
    )
    validation_errors = models.JSONField(
        verbose_name=_('validation errors'),
        default=list,
        blank=True,
        help_text=_('One record per rejected file or tripped limit, empty when the revision is valid.'),
    )
    activated = models.DateTimeField(
        verbose_name=_('activated'),
        blank=True,
        null=True,
    )

    class Meta:
        app_label = 'netbox_custom_scripts'
        ordering = ('-created',)
        verbose_name = _('custom script project revision')
        verbose_name_plural = _('custom script project revisions')
        constraints = [
            # Partial, so invalid revisions (digest NULL) coexist while valid content dedupes.
            models.UniqueConstraint(
                fields=('project', 'digest'),
                condition=Q(digest__isnull=False),
                name='unique_project_digest',
            ),
        ]

    def __str__(self):
        if self.digest:
            return f'{self.project} @ {self.digest[:12]}'
        return f'{self.project} @ {self.status}'

    def save(self, *args, **kwargs):
        """Persist the revision, refusing any change to a content field after creation."""
        # No form or serializer exposes these fields, so save() is where the invariant
        # lives. QuerySet.update() bypasses it, as with the project's data_path.
        if not self._state.adding:
            frozen = ('project_id', 'digest', 'manifest', 'file_count', 'total_size')
            original = type(self).objects.filter(pk=self.pk).values(*frozen).first()
            if original:
                errors = {}
                if original['project_id'] != self.project_id:
                    errors['project'] = _('The project cannot be changed once the revision has been created.')
                if original['digest'] != self.digest:
                    errors['digest'] = _('The digest cannot be changed once the revision has been created.')
                if original['manifest'] != self.manifest:
                    errors['manifest'] = _('The manifest cannot be changed once the revision has been created.')
                if original['file_count'] != self.file_count:
                    errors['file_count'] = _('The file count cannot be changed once the revision has been created.')
                if original['total_size'] != self.total_size:
                    errors['total_size'] = _('The total size cannot be changed once the revision has been created.')
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)
