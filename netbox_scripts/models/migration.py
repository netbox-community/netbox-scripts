from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import DEFAULT_DB_ALIAS, models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django_pg_utils import advisory_lock

from netbox.models import ChangeLoggedModel

from ..choices import MigrationStateChoices
from ..storage.locks import ADVISORY_LOCK_NAMESPACE

__all__ = ('MigrationRun', 'migration_lock')

# A namespace of its own, one above the project keyspace, so a key derived from a storage_key
# can never collide with this fixed one. Derived, so moving ADVISORY_LOCK_NAMESPACE moves this
# with it, which is why docs/development/netbox-internals.md records both.
MIGRATION_LOCK_NAMESPACE = ADVISORY_LOCK_NAMESPACE + 1
# There is at most one open migration, so the lock is on the concept rather than on a row.
MIGRATION_LOCK_KEY = 1


def migration_lock(*, using=DEFAULT_DB_ALIAS):
    """
    Hold the serialization lock for the migration run row and its journal.

    Acquisition waits for as long as another holder keeps it. The same primitive and the same
    release contract as the per-project lock in storage/locks.py.
    """
    # Not in storage/locks.py, whose lock is keyed per project and scoped to stored content,
    # while this one serializes a database row no project owns.
    return advisory_lock((MIGRATION_LOCK_NAMESPACE, MIGRATION_LOCK_KEY), using=using)


# The only order a run may move through, one step at a time. A run in the last state is closed.
_STATE_ORDER = (
    MigrationStateChoices.LEGACY,
    MigrationStateChoices.STAGING,
    MigrationStateChoices.CUTOVER,
    MigrationStateChoices.MIGRATED,
)


class MigrationRun(ChangeLoggedModel):
    """
    One attempt at migrating an installation off the built-in Custom Scripts feature.

    The row carries the migration state, the journal each cutover step replays from, and what the
    steps recorded. At most one run is open at a time, and its state only moves forward, because
    crossing into cutover cannot be undone. Abandoning a migration before that point needs no
    state of its own: the run stays in staging, which is repeatable. The cutover state is recorded
    before the first irreversible act, so it means the fence may have fired rather than that it
    finished, and only the recorded step says it completed.

    This is migration infrastructure rather than a domain model, so it carries no REST or GraphQL
    surface and no list view, and it can be removed once the v5.0 upgrade guard has run.
    """

    state = models.CharField(
        verbose_name=_('state'),
        max_length=50,
        choices=MigrationStateChoices,
        default=MigrationStateChoices.LEGACY,
        editable=False,
    )
    netbox_version = models.CharField(
        verbose_name=_('NetBox version'),
        max_length=50,
        blank=True,
        editable=False,
        help_text=_('The NetBox version this migration started against.'),
    )
    plugin_version = models.CharField(
        verbose_name=_('plugin version'),
        max_length=50,
        blank=True,
        editable=False,
    )
    cutover_started = models.DateTimeField(
        verbose_name=_('cutover started'),
        blank=True,
        null=True,
        editable=False,
    )
    completed = models.DateTimeField(
        verbose_name=_('completed'),
        blank=True,
        null=True,
        editable=False,
    )
    user = models.ForeignKey(
        to=settings.AUTH_USER_MODEL,
        verbose_name=_('user'),
        on_delete=models.SET_NULL,
        related_name='+',
        blank=True,
        null=True,
        editable=False,
    )
    journal = models.JSONField(
        verbose_name=_('journal'),
        default=dict,
        blank=True,
        editable=False,
        help_text=_(
            'What each step captured and completed. The cutover steps replay from this, so an '
            'interrupted migration resumes rather than repeats.'
        ),
    )
    warnings = models.JSONField(
        verbose_name=_('warnings'),
        default=list,
        blank=True,
        editable=False,
    )

    class Meta:
        app_label = 'netbox_scripts'
        constraints = [
            # The rule clean() states, made structural: a second open run is refused by the
            # database rather than only by a caller that remembered migration_lock().
            models.UniqueConstraint(
                models.Value(1),
                name='unique_open_migration_run',
                condition=~models.Q(state=MigrationStateChoices.MIGRATED),
            ),
        ]
        ordering = ('-created',)
        verbose_name = _('Custom Script migration')
        verbose_name_plural = _('Custom Script migrations')

    def __str__(self):
        return f'Custom Script migration {self.pk}'

    def get_absolute_url(self):
        """Return the run's own detail route."""
        # ChangeLoggedModel supplies none, and changelog rendering calls this.
        return reverse('plugins:netbox_scripts:migrationrun', args=[self.pk])

    def clean(self):
        """Validate the run, refusing a second one while another is still open."""
        super().clean()
        if self.is_closed:
            return
        open_runs = type(self).objects.exclude(pk=self.pk).exclude(state=MigrationStateChoices.MIGRATED)
        if open_runs.exists():
            raise ValidationError(
                _('Another Custom Script migration is still open. Only one migration runs at a time.')
            )

    @property
    def is_closed(self):
        """Whether this run has finished and can never move again."""
        return self.state == MigrationStateChoices.MIGRATED

    def get_state_color(self):
        """Return the badge color configured for this run's state."""
        return MigrationStateChoices.colors.get(self.state)

    @classmethod
    def current(cls):
        """Return the one open run, or None when no migration is under way."""
        # Mutating workers hold migration_lock before reading this state.
        return cls.objects.exclude(state=MigrationStateChoices.MIGRATED).order_by('-created').first()

    @classmethod
    def start(cls, user=None):
        """Open a new run in the legacy state, recording the versions it began against."""
        from .. import __version__

        # full_version rather than version, because the build is part of what an operator would
        # have to restore to, and the plain version is only what the plugin's floor check compares.
        run = cls(
            user=user,
            netbox_version=settings.RELEASE.full_version,
            plugin_version=__version__,
        )
        run.full_clean()
        run.save()
        return run

    def advance(self, state):
        """
        Move the run to the next state, refusing anything else.

        Only the state immediately after the current one is allowed, so a step cannot be skipped
        and no state is ever revisited. Raises ValidationError otherwise.
        """
        try:
            position = _STATE_ORDER.index(self.state)
        except ValueError:
            raise ValidationError(_('The run holds an unknown state and cannot be advanced.')) from None
        expected = _STATE_ORDER[position + 1] if position + 1 < len(_STATE_ORDER) else None
        if state != expected:
            raise ValidationError(
                _('A migration in the {current} state cannot move to {requested}.').format(
                    current=self.state, requested=state
                )
            )
        self.state = state
        if state == MigrationStateChoices.CUTOVER:
            self.cutover_started = timezone.now()
        elif state == MigrationStateChoices.MIGRATED:
            self.completed = timezone.now()
        self.save(update_fields=('state', 'cutover_started', 'completed', 'last_updated'))

    def record_journal(self, **entries):
        """Record journal entries, keeping whatever else the row holds."""
        with migration_lock():
            stored = self._stored('journal') or {}
            self.journal = {**stored, **entries}
            self.save(update_fields=('journal', 'last_updated'))

    def record_mapping(self, name, entries):
        """Merge explicit identities into one freshly loaded journal map."""
        with migration_lock():
            stored = self._stored('journal') or {}
            values = {**stored.get(name, {}), **entries}
            self.journal = {**stored, name: values}
            self.save(update_fields=('journal', 'last_updated'))

    def record_step(self, name, **detail):
        """Mark one step complete in the journal, carrying whatever detail a resume needs."""
        with migration_lock():
            stored = self._stored('journal') or {}
            steps = {**stored.get('steps', {}), name: {'completed': timezone.now().isoformat(), **detail}}
            self.journal = {**stored, 'steps': steps}
            self.save(update_fields=('journal', 'last_updated'))

    def record_warnings(self, warnings):
        """Record warnings on the run without marking any step complete, ignoring repeats."""
        with migration_lock():
            stored = self._stored('warnings') or []
            merged = list(stored)
            # Every pass that leaves work outstanding is re-runnable and restates what it left,
            # so without this the list would grow one copy of each message per attempt.
            for message in [*self.warnings, *map(str, warnings)]:
                if message not in merged:
                    merged.append(message)
            self.warnings = merged
            if merged != stored:
                self.save(update_fields=('warnings', 'last_updated'))

    def _stored(self, field):
        """Return one field as the database currently holds it, without disturbing this object."""
        return type(self).objects.filter(pk=self.pk).values_list(field, flat=True).first()

    def complete_step(self, name, counts, warnings=()):
        """Record one step's warnings on the run, then mark it complete with its counts."""
        self.record_warnings(warnings)
        self.record_step(name, counts=counts)

    def step_done(self, name):
        """Whether the named step has already completed in this run."""
        return name in self.journal.get('steps', {})

    def recorded_counts(self, name):
        """Return the counts the named step recorded, or an empty mapping."""
        return self.journal.get('steps', {}).get(name, {}).get('counts', {})
