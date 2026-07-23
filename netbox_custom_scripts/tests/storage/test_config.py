import pathlib
import tempfile

from django.test import TestCase, override_settings

from netbox_custom_scripts import constants
from netbox_custom_scripts.storage import config
from netbox_custom_scripts.storage.exceptions import StorageConfigurationError


def plugin_config(**settings):
    return {'netbox_custom_scripts': settings}


class StorageConfigTestCase(TestCase):
    def test_project_root_resolves_from_plugin_settings(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(PLUGINS_CONFIG=plugin_config(project_root=tmp)),
        ):
            self.assertEqual(config.get_project_root(), pathlib.Path(tmp))

    @override_settings(PLUGINS_CONFIG=plugin_config(project_root=None))
    def test_project_root_missing_raises_storage_configuration_error(self):
        with self.assertRaises(StorageConfigurationError):
            config.get_project_root()

    @override_settings(PLUGINS_CONFIG=plugin_config(project_root='relative/storage/dir'))
    def test_project_root_rejects_relative_path(self):
        with self.assertRaises(StorageConfigurationError):
            config.get_project_root()

    @override_settings(PLUGINS_CONFIG=plugin_config(project_root='/ncs/storage/does/not/exist'))
    def test_project_root_rejects_nonexistent_directory(self):
        with self.assertRaises(StorageConfigurationError):
            config.get_project_root()

    def test_runtime_cache_root_same_contract_as_project_root(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            override_settings(PLUGINS_CONFIG=plugin_config(runtime_cache_root=tmp)),
        ):
            self.assertEqual(config.get_runtime_cache_root(), pathlib.Path(tmp))
        with (
            override_settings(PLUGINS_CONFIG=plugin_config(runtime_cache_root=None)),
            self.assertRaises(StorageConfigurationError),
        ):
            config.get_runtime_cache_root()

    @override_settings(PLUGINS_CONFIG=plugin_config())
    def test_limits_fall_back_to_defaults_when_unset(self):
        self.assertEqual(config.get_max_file_size(), constants.DEFAULT_MAX_FILE_SIZE)
        self.assertEqual(config.get_max_project_size(), constants.DEFAULT_MAX_PROJECT_SIZE)
        self.assertEqual(config.get_max_file_count(), constants.DEFAULT_MAX_FILE_COUNT)

    @override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=123, max_project_size=456, max_file_count=7))
    def test_limits_honor_plugin_setting_override(self):
        self.assertEqual(config.get_max_file_size(), 123)
        self.assertEqual(config.get_max_project_size(), 456)
        self.assertEqual(config.get_max_file_count(), 7)

    def test_limits_reject_non_positive_or_non_integer_values(self):
        for bad in ('5', 1.5, True, 0, -1):
            with (
                self.subTest(bad=bad),
                override_settings(PLUGINS_CONFIG=plugin_config(max_file_size=bad)),
                self.assertRaises(StorageConfigurationError),
            ):
                config.get_max_file_size()

    def test_path_settings_reject_non_path_types(self):
        for bad in (123, ['x']):
            with (
                self.subTest(bad=bad),
                override_settings(PLUGINS_CONFIG=plugin_config(project_root=bad)),
                self.assertRaises(StorageConfigurationError),
            ):
                config.get_project_root()
