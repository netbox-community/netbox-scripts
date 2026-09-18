from django.core.files.storage import FileSystemStorage, InMemoryStorage
from django.test import TestCase, override_settings

from netbox_scripts import constants
from netbox_scripts.storage import config
from netbox_scripts.storage.exceptions import StorageConfigurationError

IN_MEMORY = {'BACKEND': 'django.core.files.storage.InMemoryStorage'}


def plugin_config(**settings):
    return {'netbox_scripts': settings}


def storages_config(**aliases):
    """Return a STORAGES setting carrying the aliases NetBox always defines plus the ones given."""
    return {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
        **aliases,
    }


class StorageConfigTestCase(TestCase):
    @override_settings(PLUGINS_CONFIG=plugin_config())
    def test_get_storage_limits_returns_all_three_values(self):
        resolved = config.get_storage_limits()
        self.assertEqual(resolved.max_file_size, constants.DEFAULT_MAX_FILE_SIZE)
        self.assertEqual(resolved.max_project_size, constants.DEFAULT_MAX_PROJECT_SIZE)
        self.assertEqual(resolved.max_file_count, constants.DEFAULT_MAX_FILE_COUNT)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=123, max_project_size=456, max_file_count=7))
    def test_get_storage_limits_reflects_overrides(self):
        resolved = config.get_storage_limits()
        self.assertEqual((resolved.max_file_size, resolved.max_project_size, resolved.max_file_count), (123, 456, 7))

    def test_limits_reject_non_positive_or_non_integer_values(self):
        for parameter in ('max_file_size', 'max_project_size', 'max_file_count'):
            message = f'The {parameter} storage setting must be a positive integer.'
            for bad in ('5', 1.5, True, 0, -1):
                with (
                    self.subTest(parameter=parameter, bad=bad),
                    override_settings(PLUGINS_CONFIG=plugin_config(**{parameter: bad})),
                    self.assertRaisesMessage(StorageConfigurationError, message),
                ):
                    config.get_storage_limits()


class StorageBackendTestCase(TestCase):
    """Cover how the backend holding revision content is chosen."""

    @override_settings(STORAGES=storages_config(netbox_scripts=IN_MEMORY))
    def test_the_configured_entry_provides_the_backend(self):
        self.assertIsInstance(config.get_storage(), InMemoryStorage)

    @override_settings(STORAGES=storages_config())
    def test_a_missing_entry_is_a_storage_configuration_error(self):
        # Executable project source must not silently inherit the media default, so an
        # unconfigured deployment refuses instead of falling back.
        with self.assertRaisesMessage(StorageConfigurationError, config.STORAGE_ALIAS):
            config.get_storage()

    def test_the_backend_is_resolved_on_each_call_rather_than_captured(self):
        file_system = {'BACKEND': 'django.core.files.storage.FileSystemStorage'}
        with override_settings(STORAGES=storages_config(netbox_scripts=IN_MEMORY)):
            self.assertIsInstance(config.get_storage(), InMemoryStorage)
        with override_settings(STORAGES=storages_config(netbox_scripts=file_system)):
            self.assertIsInstance(config.get_storage(), FileSystemStorage)

    @override_settings(STORAGES=storages_config(netbox_scripts={'BACKEND': 'nowhere.NoSuchStorage'}))
    def test_a_backend_that_cannot_be_built_is_a_storage_configuration_error(self):
        # Cleanup records this error on its job and staging raises it to the caller, so a
        # misconfigured entry has to arrive as one rather than as an import failure.
        with self.assertRaises(StorageConfigurationError):
            config.get_storage()


class StorageCheckTestCase(TestCase):
    """Cover the system check reporting an unconfigured project storage entry."""

    @override_settings(STORAGES=storages_config())
    def test_a_missing_entry_is_reported_as_w001(self):
        messages = config.check_storage_configured(None)
        self.assertEqual([message.id for message in messages], ['netbox_scripts.W001'])

    @override_settings(STORAGES=storages_config(netbox_scripts=IN_MEMORY))
    def test_a_configured_entry_reports_nothing(self):
        self.assertEqual(config.check_storage_configured(None), [])
