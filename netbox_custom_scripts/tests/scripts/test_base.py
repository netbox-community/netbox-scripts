import logging

from django.test import TestCase

from core.choices import JobNotificationChoices
from netbox_custom_scripts.scripts import (
    AbortScript,
    BaseScript,
    BooleanVar,
    IntegerVar,
    LogLevelChoices,
    Script,
    StringVar,
)


class _Linkable:
    """
    Minimal stand-in for a NetBox object with a canonical URL.
    """

    def __str__(self):
        return 'Role 7'

    def get_absolute_url(self):
        return '/dcim/device-roles/7/'


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


class ScriptMetaTestCase(TestCase):
    def test_script_subclasses_basescript(self):
        self.assertTrue(issubclass(Script, BaseScript))

    def test_meta_defaults(self):
        class TestScript(Script):
            pass

        self.assertEqual(TestScript.name, 'TestScript')
        self.assertEqual(TestScript.description, '')
        self.assertIsNone(TestScript.field_order)
        self.assertIsNone(TestScript.fieldsets)
        self.assertTrue(TestScript.commit_default)
        self.assertTrue(TestScript.scheduling_enabled)
        self.assertEqual(TestScript.notifications_default, JobNotificationChoices.NOTIFICATION_ALWAYS)
        self.assertIsNone(TestScript.job_timeout)

    def test_meta_overrides(self):
        class TestScript(Script):
            class Meta:
                name = 'Rename Devices'
                description = 'Renames devices to match the naming convention'
                field_order = ('var2', 'var1')
                fieldsets = (('Data', ('var1', 'var2')),)
                commit_default = False
                scheduling_enabled = False
                notifications_default = 'never'
                job_timeout = 600

        self.assertEqual(TestScript.name, 'Rename Devices')
        self.assertEqual(TestScript.description, 'Renames devices to match the naming convention')
        self.assertEqual(TestScript.field_order, ('var2', 'var1'))
        self.assertEqual(TestScript.fieldsets, (('Data', ('var1', 'var2')),))
        self.assertFalse(TestScript.commit_default)
        self.assertFalse(TestScript.scheduling_enabled)
        self.assertEqual(TestScript.notifications_default, 'never')
        self.assertEqual(TestScript.job_timeout, 600)

    def test_identity_properties(self):
        class TestScript(Script):
            pass

        self.assertEqual(TestScript.class_name, 'TestScript')
        self.assertEqual(TestScript.module, __name__)
        self.assertEqual(TestScript.full_name, f'{__name__}.TestScript')
        self.assertEqual(TestScript.root_module(), 'netbox_custom_scripts')

    def test_module_identity_survives_a_private_runtime_namespace(self):
        class TestScript(Script):
            pass

        # The loader will import entrypoints beneath a generated namespace and assign
        # the logical module name through _custom_script_module
        TestScript.__module__ = '_netbox_custom_scripts_runtime.p_1234.r_abcd.deploy_devices'
        TestScript._custom_script_module = 'deploy_devices'

        self.assertEqual(TestScript.module, 'deploy_devices')
        self.assertEqual(TestScript.full_name, 'deploy_devices.TestScript')
        self.assertEqual(TestScript.root_module(), 'deploy_devices')
        self.assertEqual(
            TestScript().logger.name,
            'netbox.plugins.netbox_custom_scripts.scripts.deploy_devices.TestScript',
        )

    def test_module_marker_is_not_inherited(self):
        class SharedHelperBase(BaseScript):
            pass

        # The loader marks each discovered class where it was found, so a marker
        # on a shared helper base must not become the identity of scripts built
        # on top of it
        SharedHelperBase._custom_script_module = 'helpers.common'

        class TestScript(SharedHelperBase, Script):
            pass

        self.assertEqual(SharedHelperBase.module, 'helpers.common')
        self.assertEqual(TestScript.module, __name__)
        self.assertEqual(TestScript.full_name, f'{__name__}.TestScript')
        self.assertEqual(
            TestScript().logger.name,
            f'netbox.plugins.netbox_custom_scripts.scripts.{__name__}.TestScript',
        )

    def test_str_uses_the_script_name(self):
        class TestScript(Script):
            class Meta:
                name = 'My Script'

        self.assertEqual(str(TestScript()), 'My Script')

    def test_legacy_report_and_storage_members_are_absent(self):
        class TestScript(Script):
            pass

        script = TestScript()
        for legacy in (
            'tests',
            '_current_test',
            'run_tests',
            'pre_run',
            'post_run',
            'storage',
            'filename',
            'findsource',
            'source',
        ):
            self.assertFalse(hasattr(script, legacy), f'{legacy} should not exist')


class ScriptVariableOrderingTestCase(TestCase):
    def test_source_order_is_kept(self):
        class TestScript(Script):
            var_b = StringVar(required=False)
            var_a = StringVar(required=False)
            var_c = StringVar(required=False)

        self.assertEqual(list(TestScript._get_vars()), ['var_b', 'var_a', 'var_c'])

    def test_inherited_vars_follow_the_subclass_vars(self):
        class ParentScript(Script):
            var_p = StringVar(required=False)

        class ChildScript(ParentScript):
            var_c = StringVar(required=False)

        self.assertEqual(list(ChildScript._get_vars()), ['var_c', 'var_p'])

    def test_most_derived_definition_wins(self):
        class ParentScript(Script):
            var_x = StringVar(label='parent', required=False)

        class ChildScript(ParentScript):
            var_x = StringVar(label='child', required=False)

        script_vars = ChildScript._get_vars()
        self.assertEqual(list(script_vars), ['var_x'])
        self.assertEqual(script_vars['var_x'].field_attrs['label'], 'child')

    def test_field_order_pins_listed_vars_first(self):
        class TestScript(Script):
            var_a = StringVar(required=False)
            var_b = StringVar(required=False)
            var_c = StringVar(required=False)

            class Meta:
                field_order = ('var_c', 'var_a')

        self.assertEqual(list(TestScript._get_vars()), ['var_c', 'var_a', 'var_b'])


class ScriptFormTestCase(TestCase):
    def test_default_fieldsets(self):
        class TestScript(Script):
            var1 = StringVar()
            var2 = IntegerVar()

        self.assertEqual(
            TestScript().get_fieldsets(),
            [
                ('Script Data', ['var1', 'var2']),
                ('Script Execution Parameters', ('_commit',)),
            ],
        )

    def test_meta_fieldsets_replace_the_data_group(self):
        class TestScript(Script):
            var1 = StringVar()
            var2 = IntegerVar()

            class Meta:
                fieldsets = (('First', ('var1',)), ('Second', ('var2',)))

        self.assertEqual(
            TestScript().get_fieldsets(),
            [
                ('First', ('var1',)),
                ('Second', ('var2',)),
                ('Script Execution Parameters', ('_commit',)),
            ],
        )

    def test_as_form_builds_fields_from_vars(self):
        class TestScript(Script):
            var1 = StringVar()
            var2 = BooleanVar()

        form = TestScript().as_form()
        # Django orders inherited fields before the dynamically attached ones
        self.assertEqual(list(form.fields), ['_commit', 'var1', 'var2'])

    def test_as_form_respects_commit_default(self):
        class TestScript(Script):
            class Meta:
                commit_default = False

        form = TestScript().as_form()
        self.assertFalse(form.fields['_commit'].initial)

    def test_commit_defaults_to_true(self):
        class TestScript(Script):
            pass

        form = TestScript().as_form()
        self.assertTrue(form.fields['_commit'].initial)


class ScriptLoggingTestCase(TestCase):
    def test_log_records_the_message_shape(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertLogs(script.logger, level=logging.DEBUG):
            script.log_info('Something happened')

        self.assertEqual(len(script.messages), 1)
        message = script.messages[0]
        self.assertEqual(message['status'], 'info')
        self.assertEqual(message['message'], 'Something happened')
        self.assertIsNone(message['obj'])
        self.assertIsNone(message['url'])
        self.assertIn('time', message)
        self.assertFalse(script.failed)

    def test_log_records_the_object_and_its_url(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertLogs(script.logger, level=logging.DEBUG):
            script.log_success('Renamed', obj=_Linkable())

        message = script.messages[0]
        self.assertEqual(message['status'], 'success')
        self.assertEqual(message['obj'], 'Role 7')
        self.assertEqual(message['url'], '/dcim/device-roles/7/')

    def test_log_failure_marks_the_script_failed(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertLogs(script.logger, level=logging.DEBUG):
            script.log_failure('Broke')

        self.assertTrue(script.failed)

    def test_invalid_level_raises_a_value_error(self):
        class TestScript(Script):
            pass

        with self.assertRaises(ValueError):
            TestScript()._log('Message', level='verbose')

    def test_success_forwards_to_the_system_log_as_info(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertLogs(script.logger, level=logging.DEBUG) as cm:
            script.log_success('Done')

        self.assertEqual(cm.records[0].levelno, logging.INFO)

    def test_messageless_log_is_not_recorded(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertNoLogs(script.logger, level=logging.DEBUG):
            script.log_debug()

        self.assertEqual(script.messages, [])


class ScriptExecutionSurfaceTestCase(TestCase):
    def test_run_must_be_overridden(self):
        class TestScript(Script):
            pass

        with self.assertRaises(NotImplementedError):
            TestScript().run(data={}, commit=True)

    def test_get_job_data_shape(self):
        class TestScript(Script):
            pass

        script = TestScript()
        with self.assertLogs(script.logger, level=logging.DEBUG):
            script.log_info('Hello')
        script.output = 'Some output'

        self.assertEqual(
            script.get_job_data(),
            {
                'log': script.messages,
                'output': 'Some output',
            },
        )
