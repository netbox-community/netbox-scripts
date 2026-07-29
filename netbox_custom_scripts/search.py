from netbox.search import SearchIndex, register_search

from .models.module import CustomScriptModule
from .models.project import CustomScriptProject


@register_search
class CustomScriptModuleIndex(SearchIndex):
    """Global search index for the Custom Script Module model."""

    model = CustomScriptModule
    fields = (
        ('source_path', 100),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('project', 'discovery_status', 'description')


@register_search
class CustomScriptProjectIndex(SearchIndex):
    """Global search index for the Custom Script Project model."""

    model = CustomScriptProject
    fields = (
        ('name', 100),
        ('key', 110),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('key', 'source_type', 'description')
