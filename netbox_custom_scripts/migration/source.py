"""The one place this plugin reads the built-in Custom Scripts feature."""

from dataclasses import dataclass

__all__ = (
    'LegacyModule',
    'legacy_modules',
    'read_source',
    'reference_counts',
)


@dataclass(frozen=True)
class LegacyModule:
    """One built-in script module as its row stands."""

    pk: int
    file_root: str
    file_path: str
    python_name: str
    data_source_id: int | None
    data_path: str
    script_names: tuple[str, ...]


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
            script_names=tuple(script.name for script in module.scripts.all()),
        )
        for module in ScriptModule.objects.prefetch_related('scripts').order_by('file_root', 'file_path')
    ]


def read_source(module):
    """Return one built-in module's stored bytes."""
    from django.core.files.storage import storages

    # file_path, not full_path: full_path prefixes a root that only a filesystem backend has.
    with storages['scripts'].open(module.file_path, 'rb') as handle:
        return handle.read()


def reference_counts():
    """Return how many Event Rules, permissions and Jobs reference the built-in feature."""
    from core.models import Job, ObjectType
    from extras.models import EventRule, Script, ScriptModule
    from users.models import ObjectPermission

    # Jobs and Event Rules record the proxy, so resolving the concrete model alone reports zero.
    types = [ObjectType.objects.get_for_model(model, for_concrete_model=False) for model in (Script, ScriptModule)]
    jobs = Job.objects.filter(object_type__in=types)
    return {
        'event_rules': EventRule.objects.filter(action_object_type__in=types).count(),
        'permissions': ObjectPermission.objects.filter(object_types__in=types).distinct().count(),
        'jobs': jobs.count(),
        'scheduled_jobs': jobs.filter(scheduled__isnull=False).count(),
        'recurring_jobs': jobs.filter(interval__isnull=False).count(),
    }
