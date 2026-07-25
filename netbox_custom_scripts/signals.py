"""
Cross-model side effects for the Custom Scripts plugin.

Deleting a Custom Script Project or one of its revisions reclaims the matching directory
from project storage. Each removal runs on transaction commit, so a delete that is rolled
back leaves the stored tree in place, and each is registered as robust so one failing
removal cannot stop the others queued by the same transaction.

Which directory to remove is read from the database while the row still exists, in a
pre_delete receiver, and the post_delete receiver uses nothing else. The instance handed to
a delete can be stale or carry unsaved changes, and a storage key or digest taken from it
would then name another object's content.

Each callback is registered on the connection that performed the delete, so a plugin that
routes these models to a connection of its own still gets cleanup at the right commit rather
than immediately on the default connection.

Cleanup is best effort by nature: the database rows are gone once the transaction commits,
so a failed removal has nothing left to retry from. Reclaiming what these callbacks miss is
the job of a future housekeeping reconciler.
"""

import logging

from django.db import transaction
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from . import branching
from .models import CustomScriptProject, CustomScriptProjectRevision
from .storage import config, store
from .storage.exceptions import StorageConfigurationError, StorageError

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.storage')

# Where a pre_delete receiver parks the identity its post_delete partner will need.
CLEANUP_ATTRIBUTE = '_storage_cleanup'


def _remove(operation, *arguments):
    """
    Run one storage removal, absorbing the failures that must not stop later cleanups.

    The database rows are already gone by the time this runs, so there is nothing left to
    retry from and nothing is gained by letting the exception escape. Both outcomes are
    logged at warning level, because a directory that survives its object is a leak that
    needs an operator or a future reconciler to clear.

    Unsafe branching routing is the one case logged at error level rather than warning. The
    others are refusals or gaps that leave content behind by design, while this one means the
    deleted row may not have been the only row naming this source, so removing it could take
    away content another schema still serves. Leaking a directory is recoverable, deleting live
    source is not.
    """
    if reason := branching.unsafe_routing_reason():
        logger.error(
            'Skipping storage cleanup and leaving source on disk, because %s Removing it could '
            'take away content another schema still serves: %s',
            reason,
            arguments,
        )
        return
    try:
        project_root = config.get_project_root()
    except StorageConfigurationError:
        logger.warning('Skipping storage cleanup because project storage is not configured: %s', arguments)
        return
    try:
        operation(project_root, *arguments)
    except (OSError, StorageError):
        # StorageError covers a refusal, such as a symbolic link standing where a storage
        # directory belongs. That content is deliberately left alone for an operator.
        logger.warning('Storage cleanup failed and left content on disk: %s', arguments, exc_info=True)


def _captured(instance, sender):
    """Return what a pre_delete receiver recorded, or None once it has warned about the gap."""
    captured = getattr(instance, CLEANUP_ATTRIBUTE, None)
    if captured is None:
        logger.warning(
            'Skipping storage cleanup for a deleted %s because its stored identity was not captured.',
            sender._meta.verbose_name,
        )
    return captured


@receiver(pre_delete, sender=CustomScriptProjectRevision, dispatch_uid='netbox_custom_scripts.capture_revision')
def capture_revision_storage(sender, instance, using, **kwargs):
    """
    Record the identity a deleted revision's cleanup will need, taken from its own row.

    A cascade sends every pre_delete before it deletes any row, so the owning project is
    still readable here even when it is being deleted in the same pass. Reading the project
    in post_delete instead would depend on the collector's ordering.
    """
    setattr(
        instance,
        CLEANUP_ATTRIBUTE,
        CustomScriptProjectRevision.objects.using(using)
        .filter(pk=instance.pk)
        .values_list('digest', 'project__storage_key')
        .first(),
    )


@receiver(post_delete, sender=CustomScriptProjectRevision, dispatch_uid='netbox_custom_scripts.cleanup_revision')
def cleanup_revision_storage(sender, instance, using, **kwargs):
    """Remove a deleted revision's directory once the delete commits."""
    captured = _captured(instance, sender)
    if captured is None:
        return
    digest, storage_key = captured
    # An invalid revision carries no digest and was never written to disk.
    if not digest:
        return
    transaction.on_commit(
        lambda: _remove(store.delete_revision_directory, storage_key, digest), using=using, robust=True
    )


@receiver(pre_delete, sender=CustomScriptProject, dispatch_uid='netbox_custom_scripts.capture_project')
def capture_project_storage(sender, instance, using, **kwargs):
    """Record the storage key a deleted project's cleanup will need, taken from its own row."""
    setattr(
        instance,
        CLEANUP_ATTRIBUTE,
        CustomScriptProject.objects.using(using).filter(pk=instance.pk).values_list('storage_key', flat=True).first(),
    )


@receiver(post_delete, sender=CustomScriptProject, dispatch_uid='netbox_custom_scripts.cleanup_project')
def cleanup_project_storage(sender, instance, using, **kwargs):
    """Remove a deleted project's whole storage tree once the delete commits."""
    storage_key = _captured(instance, sender)
    if storage_key is None:
        return
    transaction.on_commit(lambda: _remove(store.delete_project_directory, storage_key), using=using, robust=True)
