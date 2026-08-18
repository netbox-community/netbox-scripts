import uuid

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, router, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _

from netbox.models import ChangeLoggedModel, PrimaryModel

from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices, RevisionStatusChoices
from ..constants import ACTIVATABLE_REVISION_STATUSES
from ..storage.entrypoints import EMPTY_SNAPSHOT_DIGEST
from ..utils import data_source_relative_path
from ..validators import data_paths_overlap, normalize_data_path

# What the detail view says about a newest revision that is not the active one. Phrased for an
# operator asking "is my script live, and if not why", so it names the step in progress rather
# than the status value the badge already shows.
_SOURCE_STATE_SUMMARIES = {
    RevisionStatusChoices.STAGING: _('New source is being stored.'),
    RevisionStatusChoices.STORAGE_FAILED: _('New source could not be stored.'),
    RevisionStatusChoices.MATERIALIZED: _('New source is waiting to be validated.'),
    RevisionStatusChoices.VALIDATING: _('New source is being validated.'),
    RevisionStatusChoices.VALID: _('New source is valid and waiting to be activated.'),
    RevisionStatusChoices.INVALID: _('New source failed validation.'),
    RevisionStatusChoices.RETIRED: _('The newest revision has been retired.'),
}


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
    # SET_NULL rather than PROTECT: the project's own revisions cascade, so protecting one of
    # them here would have the project protect itself against its own deletion. Nothing is lost
    # by clearing instead, because a project stops serving through enabled, not through this
    # pointer.
    active_revision = models.ForeignKey(
        to='netbox_custom_scripts.CustomScriptProjectRevision',
        verbose_name=_('active revision'),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='active_revision_for',
        help_text=_('Currently active revision. Set only through the storage activation service.'),
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
        # Choosing what code a project runs is not a form of changing the row.
        permissions = (
            ('activate_customscriptproject', 'Can activate a revision of a Custom Script Project'),
            ('migrate_customscriptproject', 'Can migrate off the built-in Custom Scripts feature'),
            ('reconcile_customscriptproject', "Can reconcile a Custom Script Project's source"),
        )
        constraints = [
            models.CheckConstraint(
                name='enforce_source_ownership',
                condition=(
                    Q(
                        source_type=ProjectSourceTypeChoices.UPLOAD,
                        data_source__isnull=True,
                        data_path='',
                    )
                    | Q(
                        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
                        data_source__isnull=False,
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
        else:
            if self.data_source:
                errors['data_source'] = _('A data source applies only to data source-backed projects.')
            if self.data_path:
                errors['data_path'] = _('A data path applies only to data source-backed projects.')

        if (
            self.source_type == ProjectSourceTypeChoices.DATA_SOURCE
            and self.data_source_id
            and 'data_path' not in errors
        ):
            conflict = self._overlapping_sibling()
            if conflict is not None:
                errors['data_path'] = _(
                    'This data path overlaps with project "{name}" ({path}) on the same data source.'
                ).format(name=conflict.name, path=conflict.data_path or _('the data source root'))

        if self.active_revision_id:
            # The pointer is only ever set by the activation service, so anything else
            # reaching here is a bypass and gets the full set of checks.
            if self.active_revision.project_id != self.pk:
                errors['active_revision'] = _('The active revision must belong to this project.')
            elif self.active_revision.status != RevisionStatusChoices.ACTIVE:
                errors['active_revision'] = _('Only a revision with the active status can be the active revision.')
            elif not self.active_revision.digest:
                errors['active_revision'] = _('The active revision must have a content digest.')

        if not self._state.adding:
            original = (
                type(self).objects.using(self._read_alias()).filter(pk=self.pk).values('key', 'source_type').first()
            )
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
            # The persisted row is read from the alias this save writes to. Reading it from
            # anywhere else compares the new value against a different database.
            using = kwargs.get('using') or self._state.db or router.db_for_write(type(self), instance=self)
            original = (
                type(self).objects.using(using).filter(pk=self.pk).values('key', 'source_type', 'storage_key').first()
            )
            if original:
                errors = {}
                if original['key'] != self.key:
                    errors['key'] = _('The project key cannot be changed once the project has been created.')
                if original['source_type'] != self.source_type:
                    errors['source_type'] = _('The source type cannot be changed once the project has been created.')
                storage_field = self._meta.get_field('storage_key')
                if storage_field.to_python(original['storage_key']) != storage_field.to_python(self.storage_key):
                    errors['storage_key'] = _('The storage key is immutable.')
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)

    def get_source_type_color(self):
        """Return the badge color configured for this project's source type."""
        return ProjectSourceTypeChoices.colors.get(self.source_type)

    def get_activation_policy_color(self):
        """Return the badge color configured for this project's activation policy."""
        return ActivationPolicyChoices.colors.get(self.activation_policy)

    def entrypoint_candidates(self):
        """
        Return the importable modules of this project's source, at any depth.

        Nothing is imported to build the list, so listing candidates never runs project code.
        """
        return sorted(path for path in self._source_paths() if path.endswith('.py'))

    def declarable_entrypoints(self):
        """Return the candidates plus the already-declared paths, which stay selectable."""
        declared = set(self.modules.using(self._read_alias()).values_list('source_path', flat=True))
        return sorted(declared.union(self.entrypoint_candidates()))

    def select_entrypoints(self, paths):
        """
        Reconcile the declarations onto the given paths, as `enabled` rather than row deletion.

        Raises ValidationError for a path this project cannot declare.
        """
        from .module import CustomScriptModule

        selected = set(paths)
        if unknown := selected.difference(self.declarable_entrypoints()):
            raise ValidationError(
                {
                    'entrypoints': _('This project has no source file at {paths}.').format(
                        paths=', '.join(f'"{path}"' for path in sorted(unknown))
                    )
                }
            )

        using = router.db_for_write(type(self), instance=self)
        with transaction.atomic(using=using):
            existing = {module.source_path: module for module in self.modules.using(using).select_for_update().all()}
            for path in sorted(selected.difference(existing)):
                module = CustomScriptModule(project=self, source_path=path, enabled=True)
                module.full_clean()
                module.save(using=using)
            for path, module in existing.items():
                enabled = path in selected
                if module.enabled != enabled:
                    module.enabled = enabled
                    module.save(using=using, update_fields=('enabled', 'last_updated'))

    def activatable_revision(self):
        """
        Return the newest revision this project could be pointed at, or None.

        A revision that passed validation and is not already the active one. Retired revisions
        qualify, so pointing back at a previous one is a matter of choosing it rather than of
        the lifecycle allowing it. Activation re-checks everything under its own lock, so this
        answers "is there anything to offer" and never decides the outcome.
        """
        candidates = self.revisions.using(self._read_alias()).filter(status__in=ACTIVATABLE_REVISION_STATUSES)
        if self.active_revision_id:
            candidates = candidates.exclude(pk=self.active_revision_id)
        return candidates.order_by('-created').first()

    def latest_revision(self):
        """
        Return this project's newest revision whatever its state, or None.

        Unfiltered, unlike current_revision.
        """
        # The newest attempt is what the source state reports on, and a rejected one has no digest.
        return self.revisions.using(self._read_alias()).order_by('-created').first()

    @property
    def source_state(self):
        """A short plain-language summary of where this project's source stands."""
        latest = self.latest_revision()
        if latest is None:
            return _('No source has been added yet.')
        if latest.pk == self.active_revision_id:
            return _('The active revision is the newest source.')
        return _SOURCE_STATE_SUMMARIES.get(latest.status, _('A newer revision exists.'))

    @cached_property
    def current_revision(self):
        """
        The revision whose tree is this project's source right now, or None.

        The active revision when there is one, otherwise the newest revision that holds stored
        content, so a project with no active revision still has a tree to enumerate and build on.
        Cached per instance.
        """
        # The detail view reads a field of it per panel row, and every caller reloads or wants it as is.
        return self.active_revision or (
            self.revisions.using(self._read_alias()).filter(digest__isnull=False).order_by('-created').first()
        )

    def _source_paths(self):
        """Return every project-relative path of the source this project currently has."""
        # A data source is readable before anything is staged, so it wins over the manifest.
        if self.source_type == ProjectSourceTypeChoices.DATA_SOURCE and self.data_source_id:
            # Only the paths, never the content: the Entrypoints tab calls this on every render.
            return [
                relative
                for path in self.data_source.datafiles.values_list('path', flat=True)
                if (relative := data_source_relative_path(path, self.data_path)) is not None
            ]
        revision = self.current_revision
        return [entry['path'] for entry in revision.manifest] if revision else []

    def _read_alias(self):
        """Return the alias this instance's persisted state should be read from."""
        return self._state.db or router.db_for_read(type(self), instance=self)

    def _overlapping_sibling(self):
        """Return a project on the same Data Source whose path contains or sits under this one's, if any."""
        siblings = (
            type(self)
            .objects.using(self._read_alias())
            .filter(
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
    ran on. A digest addresses accepted source content and is set as soon as the
    manifest is accepted, well before project validation judges the tree fit to
    execute. A staging attempt whose content was rejected is kept with a null digest so
    its errors and partial manifest stay inspectable, and so two different broken trees
    that happen to share an accepted subset cannot collide on one digest.

    Revision identity is the project, the source digest, and the entrypoint digest. The
    snapshot freezes the enabled Module declarations staging saw, so a validation verdict
    keeps meaning when the live declarations change, and the same source tree under a
    changed configuration is a new, separately validatable revision that reuses the
    stored content.
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
        help_text=_('Content address of the source tree, set once its manifest is accepted.'),
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
        help_text=_(
            'Records from the most recent storage or validation step. An empty list does not by '
            'itself mean the revision is valid.'
        ),
    )
    discovered_scripts = models.JSONField(
        verbose_name=_('discovered scripts'),
        default=list,
        blank=True,
        help_text=_(
            'Custom Scripts this revision published, in publication order. Written once, when the '
            'revision becomes valid.'
        ),
    )
    entrypoint_snapshot = models.JSONField(
        verbose_name=_('entrypoint snapshot'),
        default=list,
        blank=True,
        help_text=_('Enabled Module declarations frozen at staging time, sorted by source path.'),
    )
    entrypoint_digest = models.CharField(
        verbose_name=_('entrypoint digest'),
        max_length=64,
        default=EMPTY_SNAPSHOT_DIGEST,
        validators=[
            RegexValidator(
                regex=r'^[0-9a-f]{64}$',
                message=_('The entrypoint digest must be 64 lowercase hexadecimal characters.'),
            )
        ],
        help_text=_('Content address of the entrypoint snapshot, part of the revision identity.'),
    )
    validation_job = models.ForeignKey(
        to='core.Job',
        verbose_name=_('validation job'),
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='+',
        editable=False,
        help_text=_('Owner of the current validation lease. Fences every final status transition.'),
    )
    validation_started = models.DateTimeField(
        verbose_name=_('validation started'),
        blank=True,
        null=True,
        editable=False,
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
            # One source tree under a changed entrypoint configuration is a separate,
            # separately validatable identity that reuses the stored content.
            models.UniqueConstraint(
                fields=('project', 'digest', 'entrypoint_digest'),
                condition=Q(digest__isnull=False),
                name='unique_project_digest_entrypoints',
            ),
            # Only a rejected staging attempt lacks a content address. Every other status
            # follows accepted content, so the row carries the digest that addresses it, and
            # no validator or loader can be handed a revision with nothing to read.
            models.CheckConstraint(
                condition=Q(status=RevisionStatusChoices.INVALID) | Q(digest__isnull=False),
                name='revision_requires_digest_unless_invalid',
            ),
            models.UniqueConstraint(
                fields=('project',),
                condition=Q(status=RevisionStatusChoices.ACTIVE),
                name='unique_active_revision_per_project',
            ),
        ]

    def __str__(self):
        if self.digest:
            return f'{self.project} @ {self.digest[:12]}'
        return f'{self.project} @ {self.status}'

    def get_absolute_url(self):
        """Return the revision's own detail route."""
        # ChangeLoggedModel supplies none, unlike the base the other three models here use.
        return reverse('plugins:netbox_custom_scripts:customscriptprojectrevision', args=[self.pk])

    def save(self, *args, **kwargs):
        """Persist the revision, refusing any change to a content field after creation."""
        # No form or serializer exposes these fields, so save() is where the invariant
        # lives. QuerySet.update() bypasses it, as with the project's data_path.
        if not self._state.adding:
            # Read the persisted row from the alias this save writes to, not from whichever
            # one a router would pick for a read.
            using = kwargs.get('using') or self._state.db or router.db_for_write(type(self), instance=self)
            frozen = (
                'project_id',
                'digest',
                'manifest',
                'file_count',
                'total_size',
                'entrypoint_snapshot',
                'entrypoint_digest',
            )
            original = type(self).objects.using(using).filter(pk=self.pk).values(*frozen).first()
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
                if original['entrypoint_snapshot'] != self.entrypoint_snapshot:
                    errors['entrypoint_snapshot'] = _(
                        'The entrypoint snapshot cannot be changed once the revision has been created.'
                    )
                if original['entrypoint_digest'] != self.entrypoint_digest:
                    errors['entrypoint_digest'] = _(
                        'The entrypoint digest cannot be changed once the revision has been created.'
                    )
                if errors:
                    raise ValidationError(errors)
        super().save(*args, **kwargs)

    @property
    def short_digest(self):
        """The digest prefix a revision is referred to by, empty for a rejected staging."""
        return self.digest[:12] if self.digest else ''

    @property
    def problems(self):
        """One row per recorded problem, in the single shape a reader needs."""
        # Storage names the rejected file "path" and validation the entrypoint "source_path".
        # Normalizing here keeps that split out of every reader.
        return [
            {
                'path': record.get('source_path') or record.get('path') or '',
                'code': record.get('code') or '',
                'message': record.get('message') or '',
                'traceback': record.get('traceback') or '',
            }
            for record in self.validation_errors
        ]

    @property
    def is_active(self):
        """Whether this revision is the one its project is serving."""
        # The status alone answers it. One active revision per project is a database
        # constraint, and promotion moves the status and the project's pointer together.
        return self.status == RevisionStatusChoices.ACTIVE

    @property
    def is_activatable(self):
        """Whether this revision's status allows it to be put into service."""
        return self.status in ACTIVATABLE_REVISION_STATUSES

    def get_status_color(self):
        """Return the badge color configured for this revision's status."""
        return RevisionStatusChoices.colors.get(self.status)
