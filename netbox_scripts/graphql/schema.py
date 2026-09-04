import strawberry
import strawberry_django

from .types import (
    CustomScriptModuleType,
    CustomScriptType,
    ScriptProjectRevisionType,
    ScriptProjectType,
)


@strawberry.type(name='Query')
class NetBoxScriptsQuery:
    """GraphQL query fields contributed by the Custom Scripts plugin."""

    custom_script: CustomScriptType = strawberry_django.field()
    custom_script_list: list[CustomScriptType] = strawberry_django.field()
    custom_script_module: CustomScriptModuleType = strawberry_django.field()
    custom_script_module_list: list[CustomScriptModuleType] = strawberry_django.field()
    netbox_script_project: ScriptProjectType = strawberry_django.field()
    netbox_script_project_list: list[ScriptProjectType] = strawberry_django.field()
    netbox_script_project_revision: ScriptProjectRevisionType = strawberry_django.field()
    netbox_script_project_revision_list: list[ScriptProjectRevisionType] = strawberry_django.field()


schema = [NetBoxScriptsQuery]
