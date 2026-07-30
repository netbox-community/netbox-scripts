import strawberry
import strawberry_django

from .types import CustomScriptModuleType, CustomScriptProjectType, CustomScriptType


@strawberry.type(name='Query')
class NetboxCustomScriptsQuery:
    """GraphQL query fields contributed by the Custom Scripts plugin."""

    custom_script: CustomScriptType = strawberry_django.field()
    custom_script_list: list[CustomScriptType] = strawberry_django.field()
    custom_script_module: CustomScriptModuleType = strawberry_django.field()
    custom_script_module_list: list[CustomScriptModuleType] = strawberry_django.field()
    custom_script_project: CustomScriptProjectType = strawberry_django.field()
    custom_script_project_list: list[CustomScriptProjectType] = strawberry_django.field()


schema = [NetboxCustomScriptsQuery]
