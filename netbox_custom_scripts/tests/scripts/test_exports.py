from django.test import TestCase

import netbox_custom_scripts
from netbox_custom_scripts import scripts


class AuthoringExportsTestCase(TestCase):
    def test_the_public_surface_is_frozen(self):
        self.assertEqual(
            scripts.__all__,
            (
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
            ),
        )

    def test_top_level_all_matches_the_scripts_package(self):
        self.assertEqual(netbox_custom_scripts.__all__, scripts.__all__)

    def test_every_public_name_resolves_to_the_scripts_package(self):
        for name in scripts.__all__:
            with self.subTest(name=name):
                self.assertIs(getattr(netbox_custom_scripts, name), getattr(scripts, name))

    def test_the_documented_author_import_works(self):
        from netbox_custom_scripts import Script, StringVar

        self.assertIs(Script, scripts.Script)
        self.assertIs(StringVar, scripts.StringVar)

    def test_dir_includes_the_authoring_api(self):
        listed = dir(netbox_custom_scripts)
        for name in scripts.__all__:
            self.assertIn(name, listed)

    def test_unknown_attribute_raises_attribute_error(self):
        with self.assertRaises(AttributeError):
            getattr(netbox_custom_scripts, 'no_such_name')

    def test_the_plugin_config_is_untouched(self):
        self.assertIs(netbox_custom_scripts.config, netbox_custom_scripts.AppConfig)
        self.assertEqual(netbox_custom_scripts.config.name, 'netbox_custom_scripts')
