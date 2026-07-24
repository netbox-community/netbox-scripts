import strawberry
import strawberry_django

from .types import CustomScriptProjectType


@strawberry.type(name='Query')
class NetboxCustomScriptsQuery:
    """GraphQL query fields contributed by the Custom Scripts plugin."""

    custom_script_project: CustomScriptProjectType = strawberry_django.field()
    custom_script_project_list: list[CustomScriptProjectType] = strawberry_django.field()


schema = [NetboxCustomScriptsQuery]
