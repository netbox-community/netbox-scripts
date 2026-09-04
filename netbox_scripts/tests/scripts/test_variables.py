from datetime import UTC, date, datetime
from decimal import Decimal

from django import forms
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from netaddr import IPAddress, IPNetwork

from dcim.models import DeviceRole
from netbox_scripts.scripts import (
    BooleanVar,
    ChoiceVar,
    DateTimeVar,
    DateVar,
    DecimalVar,
    FileVar,
    IntegerVar,
    IPAddressVar,
    IPAddressWithMaskVar,
    IPNetworkVar,
    MultiChoiceVar,
    MultiObjectVar,
    ObjectVar,
    Script,
    ScriptVariable,
    StringVar,
    TextVar,
)
from utilities.forms.widgets import DatePicker, DateTimePicker

PROTOCOL_CHOICES = (
    ('ssh', 'SSH'),
    ('telnet', 'Telnet'),
    ('console', 'Console'),
)


def make_device_roles():
    """
    Create four device roles named Test Role A through Test Role D.
    """
    for letter in 'abcd':
        DeviceRole(name=f'Test Role {letter.upper()}', slug=f'test-role-{letter}').save()


class ScriptVariablesTestCase(TestCase):
    """
    Each variable type must reject values violating its constraints and clean valid
    input to the expected Python type.
    """

    def test_stringvar(self):
        class TestScript(Script):
            hostname = StringVar(min_length=4, max_length=10, regex=r'^leaf-')

        cases = (
            ('lf1', False),  # shorter than min_length
            ('leaf-01-rack99', False),  # longer than max_length
            ('spine-01', False),  # regex mismatch
            ('leaf-01', True),
        )
        for value, valid in cases:
            with self.subTest(value=value):
                form = TestScript().as_form({'hostname': value}, None)
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.cleaned_data['hostname'], value)
                else:
                    self.assertIn('hostname', form.errors)

    def test_textvar(self):
        class TestScript(Script):
            motd = TextVar()

        banner = 'Maintenance window Friday 22:00 UTC.'
        form = TestScript().as_form({'motd': banner}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['motd'], banner)

    def test_integervar(self):
        class TestScript(Script):
            vlan_id = IntegerVar(min_value=100, max_value=199)

        cases = (
            (99, False),
            (200, False),
            (150, True),
        )
        for value, valid in cases:
            with self.subTest(value=value):
                form = TestScript().as_form({'vlan_id': value}, None)
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.cleaned_data['vlan_id'], value)
                else:
                    self.assertIn('vlan_id', form.errors)

    def test_integervar_zero_bounds(self):
        class TestScript(Script):
            offset = IntegerVar(min_value=0, max_value=0, required=False)

        # Zero bounds must be applied, not discarded
        for bad in (-1, 1):
            with self.subTest(value=bad):
                form = TestScript().as_form({'offset': bad}, None)
                self.assertFalse(form.is_valid())
                self.assertIn('offset', form.errors)

        form = TestScript().as_form({'offset': 0}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['offset'], 0)

    def test_decimalvar(self):
        class TestScript(Script):
            amps = DecimalVar(min_value=-10.5, max_value=10.5, max_digits=4, decimal_places=2, required=False)
            ratio = DecimalVar(max_digits=2, decimal_places=1, required=False)

        # Value bounds on amps
        for bad in ('-10.51', '10.51'):
            with self.subTest(value=bad):
                form = TestScript().as_form({'amps': bad}, None)
                self.assertFalse(form.is_valid())
                self.assertIn('amps', form.errors)

        # Digit and decimal-place limits on ratio
        for bad in ('12.3', '1.23'):
            with self.subTest(value=bad):
                form = TestScript().as_form({'ratio': bad}, None)
                self.assertFalse(form.is_valid())
                self.assertIn('ratio', form.errors)

        form = TestScript().as_form({'amps': '9.25'}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['amps'], Decimal('9.25'))

    def test_decimalvar_zero_constraints(self):
        class TestScript(Script):
            whole_units = DecimalVar(min_value=0, max_digits=3, decimal_places=0, required=False)

        # Zero-valued constraints must be applied, not discarded
        for bad in ('-1', '1.5'):
            with self.subTest(value=bad):
                form = TestScript().as_form({'whole_units': bad}, None)
                self.assertFalse(form.is_valid())
                self.assertIn('whole_units', form.errors)

        form = TestScript().as_form({'whole_units': '0'}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['whole_units'], Decimal('0'))

    def test_booleanvar(self):
        class TestScript(Script):
            enabled = BooleanVar()

        for flag in (True, False):
            with self.subTest(value=flag):
                form = TestScript().as_form({'enabled': flag}, None)
                self.assertTrue(form.is_valid())
                self.assertIs(form.cleaned_data['enabled'], flag)

    def test_choicevar(self):
        class TestScript(Script):
            protocol = ChoiceVar(choices=PROTOCOL_CHOICES)

        form = TestScript().as_form({'protocol': 'ssh'})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['protocol'], 'ssh')

        form = TestScript().as_form({'protocol': 'rlogin'})
        self.assertFalse(form.is_valid())

    def test_multichoicevar(self):
        class TestScript(Script):
            protocols = MultiChoiceVar(choices=PROTOCOL_CHOICES)

        form = TestScript().as_form({'protocols': ['console']})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['protocols'], ['console'])

        form = TestScript().as_form({'protocols': ('ssh', 'console')})
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['protocols'], ['ssh', 'console'])

        form = TestScript().as_form({'protocols': 'rlogin'})
        self.assertFalse(form.is_valid())

    def test_objectvar(self):
        class TestScript(Script):
            role = ObjectVar(model=DeviceRole)

        make_device_roles()
        wanted = DeviceRole.objects.get(slug='test-role-c')

        form = TestScript().as_form({'role': wanted.pk}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['role'], wanted)

    def test_multiobjectvar(self):
        class TestScript(Script):
            roles = MultiObjectVar(model=DeviceRole)

        make_device_roles()
        wanted = list(DeviceRole.objects.filter(slug__in=('test-role-a', 'test-role-d')))

        form = TestScript().as_form({'roles': [role.pk for role in wanted]}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(list(form.cleaned_data['roles']), wanted)

    def test_filevar(self):
        class TestScript(Script):
            attachment = FileVar()

        upload = SimpleUploadedFile(name='device-list.csv', content=b'name,site\nleaf-01,dc1\n')

        form = TestScript().as_form(None, {'attachment': upload})
        self.assertTrue(form.is_valid())
        self.assertIs(form.cleaned_data['attachment'], upload)

    def test_ipaddressvar(self):
        class TestScript(Script):
            gateway = IPAddressVar()

        cases = (
            ('203.0.113', False),  # not a complete address
            ('203.0.113.5/24', False),  # mask not allowed
            ('203.0.113.5', True),
        )
        for value, valid in cases:
            with self.subTest(value=value):
                form = TestScript().as_form({'gateway': value}, None)
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.cleaned_data['gateway'], IPAddress(value))
                else:
                    self.assertIn('gateway', form.errors)

    def test_ipaddresswithmaskvar(self):
        class TestScript(Script):
            uplink_ip = IPAddressWithMaskVar()

        cases = (
            ('203.0.113', False),  # not a complete address
            ('203.0.113.5', False),  # mask required
            ('203.0.113.5/24', True),
        )
        for value, valid in cases:
            with self.subTest(value=value):
                form = TestScript().as_form({'uplink_ip': value}, None)
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.cleaned_data['uplink_ip'], IPNetwork(value))
                else:
                    self.assertIn('uplink_ip', form.errors)

    def test_ipnetworkvar(self):
        class TestScript(Script):
            subnet = IPNetworkVar()

        cases = (
            ('203.0.113', False),  # not a complete address
            ('203.0.113.5/24', False),  # host bits set
            ('203.0.113.0/24', True),
        )
        for value, valid in cases:
            with self.subTest(value=value):
                form = TestScript().as_form({'subnet': value}, None)
                self.assertEqual(form.is_valid(), valid)
                if valid:
                    self.assertEqual(form.cleaned_data['subnet'], IPNetwork(value))
                else:
                    self.assertIn('subnet', form.errors)

    def test_datevar(self):
        class TestScript(Script):
            window_start = DateVar()
            window_end = DateVar(required=False)

        form = TestScript().as_form({'window_start': 'someday'}, None)
        self.assertFalse(form.is_valid())
        self.assertIn('window_start', form.errors)

        start = date(2031, 1, 15)
        form = TestScript().as_form({'window_start': start}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['window_start'], start)
        # The optional counterpart cleans to None without erroring
        self.assertIsNone(form.cleaned_data['window_end'])

    def test_datetimevar(self):
        class TestScript(Script):
            window_start = DateTimeVar()
            window_end = DateTimeVar(required=False)

        form = TestScript().as_form({'window_start': 'soon'}, None)
        self.assertFalse(form.is_valid())
        self.assertIn('window_start', form.errors)

        start = datetime(2031, 1, 15, 6, 30, tzinfo=UTC)
        form = TestScript().as_form({'window_start': start}, None)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['window_start'], start)
        # The optional counterpart cleans to None without erroring
        self.assertIsNone(form.cleaned_data['window_end'])


class ScriptVariableFieldRenderingTestCase(TestCase):
    def test_as_field_adds_the_form_control_class(self):
        field = StringVar().as_field()
        self.assertEqual(field.widget.attrs['class'], 'form-control')

    def test_as_field_appends_to_an_existing_widget_class(self):
        var = StringVar(widget=forms.TextInput(attrs={'class': 'monospace'}))
        field = var.as_field()
        self.assertEqual(field.widget.attrs['class'], 'monospace form-control')

    def test_as_field_leaves_checkboxes_alone(self):
        field = BooleanVar().as_field()
        self.assertNotIn('class', field.widget.attrs)

    def test_datevar_does_not_mutate_the_django_field_class(self):
        # The picker must live on the variable's field, never on forms.DateField
        # itself, where it would leak into every DateField in the process. The
        # snapshot comparison keeps this test independent of prior global state.
        original_widget = forms.DateField.widget

        field = DateVar().as_field()

        self.assertIsInstance(field.widget, DatePicker)
        self.assertIs(forms.DateField.widget, original_widget)

    def test_datetimevar_does_not_mutate_the_django_field_class(self):
        original_widget = forms.DateTimeField.widget

        field = DateTimeVar().as_field()

        self.assertIsInstance(field.widget, DateTimePicker)
        self.assertIs(forms.DateTimeField.widget, original_widget)

    def test_datevar_keeps_an_author_supplied_widget(self):
        var = DateVar(widget=forms.HiddenInput())
        self.assertIsInstance(var.as_field().widget, forms.HiddenInput)

    def test_textvar_keeps_an_author_supplied_widget(self):
        var = TextVar(widget=forms.HiddenInput())
        self.assertIsInstance(var.as_field().widget, forms.HiddenInput)


class ScriptVariableExtensionTestCase(TestCase):
    def test_class_level_field_attrs_stay_instance_independent(self):
        class LimitedStringVar(ScriptVariable):
            field_attrs = {'max_length': 20}

        first = LimitedStringVar(label='First', required=False)
        second = LimitedStringVar(label='Second')

        self.assertEqual(first.field_attrs['label'], 'First')
        self.assertEqual(second.field_attrs['label'], 'Second')
        self.assertFalse(first.field_attrs['required'])
        self.assertTrue(second.field_attrs['required'])
        # Both instances keep the seeded constraint and the class dict stays untouched
        self.assertEqual(first.field_attrs['max_length'], 20)
        self.assertEqual(second.field_attrs['max_length'], 20)
        self.assertEqual(LimitedStringVar.field_attrs, {'max_length': 20})
