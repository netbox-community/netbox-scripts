"""
Cross-model side effects for the Custom Scripts plugin.

Deleting a Custom Script Project Revision reclaims its content from project storage through a
background cleanup job. The revision's identity and manifest paths are read from the database
while the row still exists, in a pre_delete receiver, and the post_delete receiver records the
cleanup Job with that payload inside the transaction that deletes the row. Deletion and
cleanup intent therefore commit or roll back together, and only the handoff to the queue
waits for the commit, inside Job.enqueue(). The committing process still performs no storage
I/O of its own, and a cleanup Job that cannot be recorded aborts the deletion, which fails
closed on the side of keeping content. A captured manifest that cannot be validated aborts
it the same way, because it is the only inventory of the keys to reclaim. That coupling
exists on the default database only, so
a deletion arriving on any other alias is refused rather than allowed to record cleanup
intent that could commit independently.

Deleting a Custom Script Project needs no receiver of its own: the cascade collects every
revision it owns, and registering these receivers rules out Django's signal-free fast-delete
path for the revision model, so each cascaded revision records its own cleanup.

A completed Data Source synchronization enqueues one reconciliation Job per project backed by
that source. The receiver only enqueues, so the committing process performs no storage I/O, and
it swallows its own failures because core sends post_sync as the last statement of
DataSource.sync() and would otherwise fail an operator's synchronization over this plugin.
"""

import logging

from django.core.exceptions import ImproperlyConfigured
from django.db import DEFAULT_DB_ALIAS
from django.db.models.signals import post_delete, pre_delete
from django.dispatch import receiver

from core.signals import post_sync

from . import branching
from .choices import ProjectSourceTypeChoices
from .jobs import ProjectReconciliationJob, ProjectStorageCleanupJob
from .models import CustomScriptProject, CustomScriptProjectRevision
from .storage.exceptions import RevisionCorruptError
from .storage.manifest import validate_manifest

logger = logging.getLogger('netbox.plugins.netbox_custom_scripts.storage')

# Where the pre_delete receiver parks the identity its post_delete partner will need.
CLEANUP_ATTRIBUTE = '_storage_cleanup'


def _captured(instance, sender):
    """Return what the pre_delete receiver recorded, or None once it has warned about the gap."""
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
    Record the identity and manifest paths a deleted revision's cleanup will need.

    Everything is read from the revision's own row rather than from the instance, because the
    instance handed to a delete can be stale or carry unsaved changes, and a storage key,
    digest, or manifest taken from it would then name another object's content. A cascade
    sends every pre_delete before it deletes any row, so the owning project is still readable
    here even when it is being deleted in the same pass.
    """
    setattr(
        instance,
        CLEANUP_ATTRIBUTE,
        CustomScriptProjectRevision.objects.using(using)
        .filter(pk=instance.pk)
        .values_list('digest', 'project__storage_key', 'manifest')
        .first(),
    )


@receiver(post_delete, sender=CustomScriptProjectRevision, dispatch_uid='netbox_custom_scripts.cleanup_revision')
def cleanup_revision_storage(sender, instance, using, **kwargs):
    """Record a deleted revision's cleanup Job inside the transaction deleting the row."""
    captured = _captured(instance, sender)
    if captured is None:
        return
    digest, storage_key, manifest = captured
    # An invalid revision carries no digest and was never written to the store.
    if not digest:
        return
    # The captured manifest is input read back from a row, and it is the only inventory of
    # the stored keys. One this receiver cannot trust aborts the delete, keeping the row and
    # its inventory, the same fail-closed side a cleanup Job that cannot be recorded lands on.
    try:
        validate_manifest(manifest, digest)
    except RevisionCorruptError as error:
        raise RevisionCorruptError(
            f'Refusing to delete revision {instance.pk}: its captured manifest cannot be '
            'trusted, and it is the only inventory naming the stored content to reclaim.',
            error.reasons,
        ) from error
    paths = [entry['path'] for entry in manifest]
    # A manifest with no entries stored no keys, so there is nothing to reclaim.
    if not paths:
        return
    # Unsafe branching routing is logged at error level rather than warning, because the
    # deleted row may not have been the only row naming this source, so removing the
    # content could take away what another schema still serves. Leaking content is
    # recoverable, deleting live source is not.
    if reason := branching.unsafe_routing_reason():
        logger.error(
            'Skipping storage cleanup and leaving source in the store, because %s Removing '
            'it could take away content another schema still serves: %s %s',
            reason,
            storage_key,
            digest,
        )
        return
    # The Job row and its queue handoff bind to the default connection in the core Job
    # API, so cleanup recorded for a delete on any other alias could commit independently.
    if using != DEFAULT_DB_ALIAS:
        raise ImproperlyConfigured(
            f'Custom Script Project revisions must live on the "{DEFAULT_DB_ALIAS}" database. '
            f'This revision was deleted on "{using}", where its cleanup Job cannot be recorded '
            'in the same transaction.'
        )
    # Since entrypoint configuration joined revision identity, several rows can reference
    # one stored tree. Content a surviving row still names is kept, the check fails on the
    # side of retaining bytes, and a project cascade that deletes every referencing row in
    # one pass enqueues per row, which the job's exact-key, already-gone-tolerant deletion
    # absorbs.
    if (
        CustomScriptProjectRevision.objects.using(using)
        .filter(project__storage_key=storage_key, digest=digest)
        .exclude(pk=instance.pk)
        .exists()
    ):
        logger.info(
            'Skipping storage cleanup for revision %s, another revision still references %s %s.',
            instance.pk,
            storage_key,
            digest,
        )
        return
    ProjectStorageCleanupJob.enqueue_cleanup(storage_key=storage_key, digest=digest, paths=paths)


@receiver(post_sync, dispatch_uid='netbox_custom_scripts.reconcile_sources')
def reconcile_project_sources(sender, instance, **kwargs):
    """
    Enqueue reconciliation for every project backed by the Data Source that just synchronized.

    Never raises, and never touches storage. Core sends this signal with send() rather than
    send_robust(), as the last statement of DataSource.sync(), so an escaping exception would
    fail an operator's own synchronization over a plugin they may barely use. A project that
    does not get its Job stays on the source it already serves until the next synchronization
    or a manual reconciliation.
    """
    try:
        projects = CustomScriptProject.objects.filter(
            source_type=ProjectSourceTypeChoices.DATA_SOURCE,
            data_source=instance,
        )
        for project in projects:
            ProjectReconciliationJob.enqueue_reconciliation(project)
    except Exception:
        logger.exception(
            'Could not enqueue Custom Script Project source reconciliation after "%s" synchronized.',
            instance,
        )
