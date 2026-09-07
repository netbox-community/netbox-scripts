from netbox.search import SearchIndex, register_search

from .models.projects import ScriptProject
from .models.scripts import NetBoxScript, ScriptFile


@register_search
class NetBoxScriptIndex(SearchIndex):
    """Global search index for the Script model."""

    model = NetBoxScript
    fields = (
        ('display_name', 100),
        ('module_path', 110),
        ('class_name', 120),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('project', 'class_name', 'description')


@register_search
class ScriptFileIndex(SearchIndex):
    """Global search index for the Script File model."""

    model = ScriptFile
    fields = (
        ('source_path', 100),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('project', 'discovery_status', 'description')


@register_search
class ScriptProjectIndex(SearchIndex):
    """Global search index for the Script Project model."""

    model = ScriptProject
    fields = (
        ('name', 100),
        ('key', 110),
        ('description', 500),
        ('comments', 5000),
    )
    display_attrs = ('key', 'source_type', 'description')
