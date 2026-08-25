"""Create the Projects a migration plan proposes and stage their content."""

from django.core.exceptions import SuspiciousOperation
from django.db import router, transaction

from .. import ingestion
from ..choices import ActivationPolicyChoices, ProjectSourceTypeChoices
from ..models import CustomScriptProject
from ..utils import data_source_relative_path
from . import dialects
from . import source as legacy_source

__all__ = ('stage',)


def stage(proposed, modules):
    """
    Create or reuse each proposed Project, declare its entrypoints, and stage its content.

    Idempotent by identity: a Data Source Project resolves on the pair its unique constraint
    already covers, an uploaded one on its deterministic key, and identical content resolves to
    the revision that already holds it. Only a member that would publish a Script is declared,
    the rest are staged as helper files. Returns one result mapping per Project.
    """
    by_pk = {module.pk: module for module in modules}
    results = []
    for project_plan in proposed:
        project, created = _project_for(project_plan)
        members = [by_pk[pk] for pk in project_plan.module_pks]
        _declare(project, members)
        staged = _stage_content(project, members)
        results.append(
            {
                'key': project.key,
                'created': created,
                'revision_pk': staged.revision.pk,
                'revision_created': staged.created,
                'revision_status': staged.revision.status,
            }
        )
    return results


def _project_for(project_plan):
    """Return the Project one plan entry resolves to, and whether this call created it."""
    existing = _existing(project_plan)
    if existing is not None:
        return existing, False
    project = CustomScriptProject(
        name=project_plan.name,
        key=project_plan.key,
        source_type=project_plan.source_type,
        data_source_id=project_plan.data_source_id,
        data_path=project_plan.data_path,
        # Manual whatever an operator may later choose, because validation activates a valid
        # revision under any other policy and the cutover is what decides who serves.
        activation_policy=ActivationPolicyChoices.MANUAL,
    )
    # Validated rather than get_or_create'd, so a data path overlapping a project an operator
    # made by hand is refused instead of becoming a row the model forbids.
    project.full_clean()
    project.save()
    return project, True


def _existing(project_plan):
    """Return the Project this plan entry already resolves to, if there is one."""
    if project_plan.source_type == ProjectSourceTypeChoices.UPLOAD:
        return CustomScriptProject.objects.filter(key=project_plan.key).first()
    return CustomScriptProject.objects.filter(
        source_type=ProjectSourceTypeChoices.DATA_SOURCE,
        data_source_id=project_plan.data_source_id,
        data_path=project_plan.data_path,
    ).first()


def _publishes(module):
    """Whether anything would publish from this module, by its built-in rows or by its source."""
    # The rows first, because that costs no read and answers the overwhelming majority.
    if any(script.is_executable for script in module.scripts):
        return True
    # The built-in feature only ever recorded a class subclassing its own base, so source already
    # written against this plugin's API holds no row while still publishing once migrated.
    try:
        return dialects.defines_a_script(legacy_source.read_source(module))
    except (OSError, SuspiciousOperation):
        # Unreadable is the inventory's business and it blocks staging there, so the safe answer
        # here is to declare and let a verdict name the file.
        return True


def _declare(project, members):
    """Declare each member that would publish a Script as an enabled entrypoint of the project."""
    # Staging freezes the project's enabled declarations into the revision, so these commit
    # first. An uploaded project needs none of this, ingest_upload declares the file it carries.
    if project.source_type == ProjectSourceTypeChoices.UPLOAD:
        return
    using = router.db_for_write(CustomScriptProject, instance=project)
    with transaction.atomic(using=using):
        for member in members:
            if not _publishes(member):
                # A declaration claims the file publishes, and a revision whose entrypoints all
                # publish nothing is refused, which would leave the Project unactivatable.
                continue
            path = data_source_relative_path(member.data_path, project.data_path)
            ingestion.declare_entrypoint(project, path, using)


def _stage_content(project, members):
    """Stage the project's source and return the StagedRevision."""
    if project.source_type == ProjectSourceTypeChoices.UPLOAD:
        member = members[0]
        return ingestion.ingest_upload(
            project,
            filename=member.file_path,
            content=legacy_source.read_source(member),
            declare=_publishes(member),
        )
    # The whole directory is staged from the Data Source's own files, so a helper beside a
    # script arrives with it even though the built-in feature never synced one.
    return ingestion.ingest_data_source(project)
