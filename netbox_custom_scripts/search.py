from netbox.search import SearchIndex, register_search

from .models.customscriptproject import CustomScriptProject


@register_search
class CustomScriptProjectIndex(SearchIndex):
    model = CustomScriptProject
    fields = (
        ('name', 100),
        ('key', 110),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('key', 'source_type', 'description')
