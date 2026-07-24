import uuid

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

from netbox.models import PrimaryModel

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices
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
