import strawberry
import strawberry_django

from .types import (
    NetBoxScriptType,
    ScriptFileType,
    ScriptProjectRevisionType,
    ScriptProjectType,
)


@strawberry.type(name='Query')
class NetBoxScriptsQuery:
    """GraphQL query fields contributed by the NetBox Scripts plugin."""

    netbox_script: NetBoxScriptType = strawberry_django.field()
    netbox_script_list: list[NetBoxScriptType] = strawberry_django.field()
    netbox_script_file: ScriptFileType = strawberry_django.field()
    netbox_script_file_list: list[ScriptFileType] = strawberry_django.field()
    netbox_script_project: ScriptProjectType = strawberry_django.field()
    netbox_script_project_list: list[ScriptProjectType] = strawberry_django.field()
    netbox_script_project_revision: ScriptProjectRevisionType = strawberry_django.field()
    netbox_script_project_revision_list: list[ScriptProjectRevisionType] = strawberry_django.field()


schema = [NetBoxScriptsQuery]
