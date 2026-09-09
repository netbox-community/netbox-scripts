"""
Resolve every NetBox internal this plugin depends on, so a removal fails CI before a release.

Each probe names a module and an attribute chain, spelled `module:attr.attr['key']`, and is
resolved after django.setup(). One that no longer resolves is reported under the row of
docs/development/netbox-internals.md it belongs to. Two further checks refuse a core advisory-lock
key that has landed on either namespace the plugin claims, and a NetBox that skipped the plugin at
settings load for being outside its version range.

Usage: `python scripts/check_netbox_internals.py --netbox /path/to/netbox/netbox`, with
NETBOX_CONFIGURATION and PYTHONPATH set as they are for manage.py. Exits non-zero on any
failure. Adding a row to the documentation page means adding a probe here, in table order.
"""

from __future__ import annotations

import argparse
import importlib
import os
import re
import sys
from pathlib import Path

# Read from the plugin once Django is up, so moving a namespace there moves this check with it.
PLUGIN_LOCK_NAMESPACES = (
    'netbox_scripts.storage.locks:ADVISORY_LOCK_NAMESPACE',
    'netbox_scripts.models.migration:MIGRATION_LOCK_NAMESPACE',
)

# One entry per table row, in table order: the row's symbol text, then one probe per thing it names.
ROWS = (
    ("netbox.registry.registry['request_processors']", "netbox.registry:registry['request_processors']"),
    ('netbox.context_managers.event_tracking', 'netbox.context_managers:event_tracking'),
    ('netbox.context.current_request', 'netbox.context:current_request'),
    ('core.signals.clear_events', 'core.signals:clear_events'),
    ('django.db.router.db_for_write on a change-logged core model', 'dcim.models:Device'),
    ('utilities.exceptions.AbortScript', 'utilities.exceptions:AbortScript'),
    ('utilities.request.copy_safe_request', 'utilities.request:copy_safe_request'),
    (
        'netbox.api.viewsets.mixins.discard_events_on_rollback',
        'netbox.api.viewsets.mixins:discard_events_on_rollback',
    ),
    ('utilities.exceptions.PermissionsViolation', 'utilities.exceptions:PermissionsViolation'),
    ('utilities.rqworker.get_queue_for_model', 'utilities.rqworker:get_queue_for_model'),
    ('rq.utils.parse_timeout', 'rq.utils:parse_timeout'),
    ('rq.exceptions.TimeoutFormatError', 'rq.exceptions:TimeoutFormatError'),
    ('rq.timeouts.JobTimeoutException', 'rq.timeouts:JobTimeoutException'),
    ('utilities.rqworker.any_workers_for_queue', 'utilities.rqworker:any_workers_for_queue'),
    ('utilities.exceptions.RQWorkerNotRunningException', 'utilities.exceptions:RQWorkerNotRunningException'),
    ('netbox.api.authentication.TokenPermissions', 'netbox.api.authentication:TokenPermissions'),
    ('extras.models.ScriptModule and its manager', 'extras.models:ScriptModule.objects'),
    ('extras.models.Script through module.scripts', 'extras.models:Script', 'extras.models:ScriptModule.scripts'),
    (
        'core.models.ManagedFile fields file_root, file_path, data_path, data_source',
        'core.models:ManagedFile.file_root',
        'core.models:ManagedFile.file_path',
        'core.models:ManagedFile.data_path',
        'core.models:ManagedFile.data_source',
    ),
    (
        'core.choices.ManagedFileRootPathChoices.SCRIPTS and .REPORTS',
        'core.choices:ManagedFileRootPathChoices.SCRIPTS',
        'core.choices:ManagedFileRootPathChoices.REPORTS',
    ),
    ('extras.models.mixins.PythonModuleMixin.python_name', 'extras.models.mixins:PythonModuleMixin.python_name'),
    ("storages['scripts']", "django.core.files.storage:storages['scripts']"),
    ('extras.models.EventRule.action_object_type', 'extras.models:EventRule.action_object_type'),
    ('users.models.ObjectPermission.object_types', 'users.models:ObjectPermission.object_types'),
    ('core.models.Job.object_type', 'core.models:Job.object_type'),
    (
        'extras.models.ScriptModule.jobs and .event_rules',
        'extras.models:ScriptModule.jobs',
        'extras.models:ScriptModule.event_rules',
    ),
    ('users.models.ObjectPermission.enabled', 'users.models:ObjectPermission.enabled'),
    ('extras.models.EventRule.enabled', 'extras.models:EventRule.enabled'),
    ('core.models.Job.terminate', 'core.models:Job.terminate'),
    (
        'django_rq.get_queue and rq.job.Job.fetch / .delete',
        'django_rq:get_queue',
        'rq.job:Job.fetch',
        'rq.job:Job.delete',
    ),
    ('rq.exceptions.NoSuchJobError', 'rq.exceptions:NoSuchJobError'),
    ('core.models.AutoSyncRecord', 'core.models:AutoSyncRecord'),
    (
        'extras.models.EventRule.action_type, .action_object_type, .action_object_id, .object_types',
        'extras.models:EventRule.action_type',
        'extras.models:EventRule.action_object_type',
        'extras.models:EventRule.action_object_id',
        'extras.models:EventRule.object_types',
    ),
    (
        'users.models.ObjectPermission creation, .actions, .object_types, .users, .groups',
        'users.models:ObjectPermission',
        'users.models:ObjectPermission.actions',
        'users.models:ObjectPermission.object_types',
        'users.models:ObjectPermission.users',
        'users.models:ObjectPermission.groups',
    ),
    ('users.models.Group', 'users.models:Group'),
    ('core.models.Job.object_type / .object_id update', 'core.models:Job.object_type', 'core.models:Job.object_id'),
    ('extras.models.ScriptModule.delete', 'extras.models:ScriptModule.delete'),
)

CHAIN = re.compile(r"(?:\w+|\['[^']+'\])(?:\.\w+|\['[^']+'\])*")
STEP = re.compile(r"\.?(\w+)|\['([^']+)'\]")


def resolve(probe):
    """
    Return None when the probe resolves, otherwise one line naming the probe and the error.

    A malformed probe raises ValueError.
    """
    module, _, chain = probe.partition(':')
    if not module or not CHAIN.fullmatch(chain):
        raise ValueError(f'malformed probe {probe!r}')
    try:
        target = importlib.import_module(module)
        for attribute, key in STEP.findall(chain):
            target = getattr(target, attribute) if attribute else target[key]
    except Exception as error:  # Whatever the type, a failure to resolve is the finding.
        return f'{probe}: {type(error).__name__}: {error}'
    return None


def check(rows=ROWS):
    """Return one line per probe that no longer resolves, prefixed by its table row."""
    return [f'{label}: {failure}' for label, *probes in rows for probe in probes if (failure := resolve(probe))]


def check_plugin_loaded():
    """Return one line when NetBox skipped the plugin at settings load for being outside its version range."""
    from django.apps import apps
    from django.conf import settings

    import netbox_scripts

    config = netbox_scripts.config
    if apps.is_installed(config.name):
        return []
    version = getattr(settings, 'VERSION', 'unknown')
    window = f'{config.min_version} to {config.max_version}'
    return [f'{config.name} was skipped at settings load on NetBox {version}, outside its declared range {window}']


def check_lock_keys():
    """Return one line per core advisory-lock key that sits on a namespace the plugin claims."""
    from netbox.constants import ADVISORY_LOCK_KEYS

    claimed = {}
    for probe in PLUGIN_LOCK_NAMESPACES:
        module, _, name = probe.partition(':')
        claimed[getattr(importlib.import_module(module), name)] = probe
    return [
        f'ADVISORY_LOCK_KEYS[{name!r}] is {value}, which {claimed[value]} claims'
        for name, value in ADVISORY_LOCK_KEYS.items()
        if value in claimed
    ]


def report(failures, version):
    """Print the outcome and return the process exit status."""
    if not failures:
        print(f'netbox-internals: OK, every listed crossing resolves on NetBox {version}.')
        return 0
    print(f'netbox-internals: FAIL on NetBox {version}.')
    print('Each line names a table row of docs/development/netbox-internals.md whose symbol no longer')
    print('resolves, a core advisory-lock key that collides with one the plugin holds, or a plugin')
    print('NetBox skipped at settings load.')
    for failure in failures:
        print(f'  {failure}')
    print(f'\n{len(failures)} failure{"s" if len(failures) != 1 else ""}.')
    return 1


def main(argv=None):
    """Set Django up against the given NetBox checkout, run every check, and report."""
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument('--netbox', required=True, type=Path, help='the netbox/ directory holding manage.py')
    args = parser.parse_args(argv)
    if not (args.netbox / 'netbox' / 'settings.py').is_file():
        parser.error(f'{args.netbox} holds no netbox/settings.py')
    sys.path.insert(0, str(args.netbox))
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'netbox.settings')
    import django

    django.setup()
    from django.conf import settings

    failures = [*check_plugin_loaded(), *check(), *check_lock_keys()]
    return report(failures, getattr(settings, 'VERSION', 'unknown'))


if __name__ == '__main__':
    sys.exit(main())
