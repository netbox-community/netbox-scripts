import logging

from django.test import TestCase

from netbox_custom_scripts.scripts import AbortScript, LogLevelChoices


class AbortScriptTestCase(TestCase):
    def test_abort_script_is_an_exception(self):
        self.assertTrue(issubclass(AbortScript, Exception))

    def test_abort_script_carries_its_message(self):
        with self.assertRaises(AbortScript) as cm:
            raise AbortScript('Stop right there')
        self.assertEqual(str(cm.exception), 'Stop right there')


class LogLevelChoicesTestCase(TestCase):
    def test_log_level_values(self):
        self.assertEqual(
            LogLevelChoices.values(),
            ['debug', 'info', 'success', 'warning', 'failure'],
        )

    def test_system_levels_cover_every_level(self):
        self.assertEqual(set(LogLevelChoices.SYSTEM_LEVELS), set(LogLevelChoices.values()))

    def test_system_level_mapping_matches_stdlib(self):
        self.assertEqual(
            LogLevelChoices.SYSTEM_LEVELS,
            {
                LogLevelChoices.LOG_DEBUG: logging.DEBUG,
                LogLevelChoices.LOG_INFO: logging.INFO,
                LogLevelChoices.LOG_SUCCESS: logging.INFO,
                LogLevelChoices.LOG_WARNING: logging.WARNING,
                LogLevelChoices.LOG_FAILURE: logging.ERROR,
            },
        )
