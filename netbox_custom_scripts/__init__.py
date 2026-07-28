__author__ = 'NetBox Labs'
__email__ = 'support@netboxlabs.com'
__version__ = '0.0.1'


from netbox.plugins import PluginConfig
from netbox_custom_scripts import constants


class AppConfig(PluginConfig):
    """NetBox Custom Scripts configuration."""

    name = 'netbox_custom_scripts'
    label = 'netbox_custom_scripts'
    verbose_name = 'NetBox Custom Scripts'
    description = 'Custom Scripts for NetBox'
    version = __version__
    author = __author__
    author_email = __email__
    base_url = 'custom-scripts'
    min_version = '4.6.0'
    max_version = '4.7.99'

    default_settings = {
        'max_file_size': constants.DEFAULT_MAX_FILE_SIZE,
        'max_project_size': constants.DEFAULT_MAX_PROJECT_SIZE,
        'max_file_count': constants.DEFAULT_MAX_FILE_COUNT,
        # The runtime cache defaults to a directory under the system temporary directory.
        'runtime_cache_root': None,
    }

    def ready(self):
        super().ready()
        from django.core.checks import register as register_check

        from netbox_custom_scripts import branching, signals  # noqa: F401
        from netbox_custom_scripts.storage import config as storage_config

        # Both models are branch-aware by default, but one project owns one source tree with
        # no branch context in its path, so branching is asked to route them to the main
        # schema. Registration is best effort: the check reports unsafe routing and the storage
        # operations refuse it, so nothing here needs to prevent NetBox from starting.
        branching.register()
        register_check(branching.check_routing)
        # The project storage entry is required. The check reports the gap and the storage
        # operations refuse it, following the same split as the branching check above.
        register_check(storage_config.check_storage_configured)


config = AppConfig

# The authoring API (netbox_custom_scripts.scripts) is re-exported lazily via PEP 562.
# Django imports this package before app setup, so nothing here may import Django form
# machinery eagerly.
__all__ = (
    'AbortScript',
    'BaseScript',
    'BooleanVar',
    'ChoiceVar',
    'DateTimeVar',
    'DateVar',
    'DecimalVar',
    'FileVar',
    'IPAddressVar',
    'IPAddressWithMaskVar',
    'IPNetworkVar',
    'IntegerVar',
    'LogLevelChoices',
    'MultiChoiceVar',
    'MultiObjectVar',
    'ObjectVar',
    'Script',
    'ScriptVariable',
    'StringVar',
    'TextVar',
)


def __getattr__(name):
    if name in __all__:
        from netbox_custom_scripts import scripts

        return getattr(scripts, name)
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}')


def __dir__():
    return sorted({*globals(), *__all__})
