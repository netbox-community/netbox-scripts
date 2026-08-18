"""The one place this plugin reads the built-in Custom Scripts feature.

Content comes back as frozen dataclasses and counts. The reference readers return live core rows
instead. What to do with them is the caller's business.
"""

from dataclasses import dataclass

__all__ = (
    'LegacyModule',
    'LegacyScript',
    'enqueued_script_jobs',
    'legacy_auto_sync_records',
    'legacy_event_rules',
    'legacy_modules',
    'legacy_modules_by_pk',
    'legacy_object_types',
    'legacy_permissions',
    'module_references',
    'read_source',
    'reference_counts',
    'running_script_jobs',
    'script_jobs',
)


@dataclass(frozen=True)
class LegacyScript:
    """One built-in Script as its row stands."""

    # The primary key is carried because every reference a migration repoints names it: an Event
    # Rule's action object, a Job's object id, and a permission's constraint all hold it.
    pk: int
    name: str
    # False once the class left the file and only its history keeps the row. It never publishes.
    is_executable: bool = True


@dataclass(frozen=True)
class LegacyModule:
    """One built-in script module as its row stands."""

    pk: int
    file_root: str
    file_path: str
    python_name: str
    data_source_id: int | None
    data_path: str
    scripts: tuple[LegacyScript, ...]


def legacy_modules():
    """Return every built-in script module, ordered by root and path."""
    from extras.models import ScriptModule

    return [
        LegacyModule(
            pk=module.pk,
            file_root=module.file_root,
            file_path=module.file_path,
            python_name=module.python_name,
            data_source_id=module.data_source_id,
            data_path=module.data_path,
            scripts=tuple(
                LegacyScript(pk=script.pk, name=script.name, is_executable=script.is_executable)
                for script in module.scripts.all()
            ),
        )
        for module in ScriptModule.objects.prefetch_related('scripts').order_by('file_root', 'file_path')
    ]


def read_source(module):
    """Return one built-in module's stored bytes."""
    from django.core.files.storage import storages

    # file_path, not full_path: full_path prefixes a root that only a filesystem backend has.
    with storages['scripts'].open(module.file_path, 'rb') as handle:
        return handle.read()


def legacy_object_types():
    """Return the object types a reference to the built-in feature can name."""
    from extras.models import Script, ScriptModule

    return [_object_type(Script), _object_type(ScriptModule)]


def _object_type(model):
    """Return the object type a reference records for one built-in model."""
    from core.models import ObjectType

    # Jobs and Event Rules record the proxy, so resolving the concrete model alone reports zero.
    return ObjectType.objects.get_for_model(model, for_concrete_model=False)


def script_jobs():
    """Return every Job that names the built-in feature, whatever its status."""
    from core.models import Job

    return Job.objects.filter(object_type__in=legacy_object_types())


def running_script_jobs():
    """Return every built-in Script job executing right now."""
    from core.choices import JobStatusChoices

    return script_jobs().filter(status=JobStatusChoices.STATUS_RUNNING)


def enqueued_script_jobs():
    """Return every built-in Script job waiting to run, one-shot, scheduled or recurring."""
    from core.choices import JobStatusChoices

    # Running is deliberately excluded: a job mid-flight is refused rather than cancelled.
    return script_jobs().filter(status__in=(JobStatusChoices.STATUS_PENDING, JobStatusChoices.STATUS_SCHEDULED))


def legacy_event_rules():
    """Return every Event Rule that names the built-in feature as its action or as a source."""
    from django.db.models import Q

    from extras.models import EventRule

    types = legacy_object_types()
    # object_types relates to ContentType while these are ObjectType rows, which share its keys.
    keys = [object_type.pk for object_type in types]
    return EventRule.objects.filter(Q(action_object_type__in=types) | Q(object_types__in=keys)).distinct()


def legacy_permissions():
    """Return every permission granting an action on the built-in feature."""
    from users.models import ObjectPermission

    return ObjectPermission.objects.filter(object_types__in=legacy_object_types()).distinct()


def legacy_auto_sync_records(module_pks=None):
    """Return the synchronization registrations that rewrite built-in script source."""
    from core.models import AutoSyncRecord, ObjectType
    from extras.models import ScriptModule

    # The opposite of the rule everywhere else here: SyncedDataMixin.save() registers with
    # get_for_model(self) and no for_concrete_model=False, so a ScriptModule's record names the
    # CONCRETE ManagedFile type. Filtering the proxy type finds nothing at all. The module keys
    # narrow it, because that type also covers every other ManagedFile an installation holds.
    concrete = ObjectType.objects.get_for_model(ScriptModule)
    keys = ScriptModule.objects.values_list('pk', flat=True) if module_pks is None else module_pks
    return AutoSyncRecord.objects.filter(object_type=concrete, object_id__in=list(keys))


def module_references(module):
    """
    Return what still refers to one built-in module, by kind, and how many Scripts it holds.

    Every kind reported here reaches the module or its Scripts through a GenericRelation, so
    Django's collector deletes those rows along with it rather than orphaning them. What to do
    about that is the caller's business.
    """
    from core.models import Job
    from extras.models import EventRule, Script

    script_pks = list(module.scripts.values_list('pk', flat=True))
    # Split by executability: no plugin row can ever hold a departed class's history.
    retired_pks = list(module.scripts.filter(is_executable=False).values_list('pk', flat=True))
    live_pks = [pk for pk in script_pks if pk not in retired_pks]
    script_type = _object_type(Script)
    held = Job.objects.filter(object_type=script_type)
    return {
        'scripts': len(script_pks),
        'module_jobs': module.jobs.exists(),
        'retired_script_jobs': held.filter(object_id__in=retired_pks).exists(),
        'live_script_jobs': held.filter(object_id__in=live_pks).exists(),
        'event_rules': module.event_rules.exists()
        or EventRule.objects.filter(action_object_type=script_type, action_object_id__in=script_pks).exists(),
    }


def legacy_modules_by_pk(keys):
    """Return the live built-in module rows for the given keys, for a caller that has to write."""
    from extras.models import ScriptModule

    return ScriptModule.objects.filter(pk__in=list(keys)).order_by('file_root', 'file_path')


def reference_counts():
    """Return how many Event Rules, permissions and Jobs reference the built-in feature."""
    jobs = script_jobs()
    return {
        'event_rules': legacy_event_rules().count(),
        'permissions': legacy_permissions().count(),
        'jobs': jobs.count(),
        'scheduled_jobs': jobs.filter(scheduled__isnull=False).count(),
        'recurring_jobs': jobs.filter(interval__isnull=False).count(),
    }
