__author__ = 'NetBox Labs'
__email__ = 'support@netboxlabs.com'
__version__ = '0.0.1'


from netbox.plugins import PluginConfig


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

    # Uncomment to wire Django signals once you ship `signals.py`:
    #
    # def ready(self):
    #     super().ready()
    #     from netbox_custom_scripts import signals


config = AppConfig
