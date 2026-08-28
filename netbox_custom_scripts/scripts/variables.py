from django import forms
from django.core.validators import RegexValidator

from ipam.formfields import IPAddressFormField, IPNetworkFormField
from ipam.validators import MaxPrefixLengthValidator, MinPrefixLengthValidator, prefix_validator
from utilities.forms import add_blank_choice
from utilities.forms.fields import DynamicModelChoiceField, DynamicModelMultipleChoiceField
from utilities.forms.widgets import DatePicker, DateTimePicker

__all__ = (
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
    'MultiChoiceVar',
    'MultiObjectVar',
    'ObjectVar',
    'ScriptVariable',
    'StringVar',
    'TextVar',
)


class ScriptVariable:
    """
    Base class for Custom Script variables.

    A variable declares one input of a script and knows how to materialize itself as a
    Django form field. Subclasses pick the field through form_field and seed
    field_attrs with the keyword arguments that field needs.
    """

    form_field = forms.CharField

    def __init__(self, label='', description='', default=None, required=True, widget=None):
        # Copy any pre-seeded attributes so every instance owns its own dictionary,
        # even when a subclass declares field_attrs at class level
        self.field_attrs = dict(getattr(self, 'field_attrs', {}))
        if label:
            self.field_attrs['label'] = label
        if description:
            self.field_attrs['help_text'] = description
        if default is not None:
            self.field_attrs['initial'] = default
        if widget:
            self.field_attrs['widget'] = widget
        self.field_attrs['required'] = required

    def as_field(self):
        """
        Build the Django form field for this variable.

        Anything but a checkbox gets NetBox's form-control CSS class appended so it
        renders styled.
        """
        field = self.form_field(**self.field_attrs)
        if not isinstance(field.widget, forms.CheckboxInput):
            if field.widget.attrs and 'class' in field.widget.attrs:
                field.widget.attrs['class'] += ' form-control'
            else:
                field.widget.attrs['class'] = 'form-control'

        return field


class StringVar(ScriptVariable):
    """A single-line text value. Length bounds and a validation regex are optional."""

    def __init__(self, min_length=None, max_length=None, regex=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        if min_length:
            self.field_attrs['min_length'] = min_length
        if max_length:
            self.field_attrs['max_length'] = max_length

        if regex:
            # The message wording is deliberately kept identical to the error text
            # existing scripts already produce
            self.field_attrs['validators'] = [
                RegexValidator(
                    regex=regex,
                    message='Invalid value. Must match regex: {}'.format(regex),
                    code='invalid',
                )
            ]


class TextVar(ScriptVariable):
    """A multi-line text value, rendered as a textarea."""

    form_field = forms.CharField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # An author-supplied widget wins over the textarea default
        self.field_attrs.setdefault('widget', forms.Textarea)


class IntegerVar(ScriptVariable):
    """A whole number, with optional lower and upper bounds."""

    form_field = forms.IntegerField

    def __init__(self, min_value=None, max_value=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Bounds are compared against None so that zero is a usable bound
        if min_value is not None:
            self.field_attrs['min_value'] = min_value
        if max_value is not None:
            self.field_attrs['max_value'] = max_value


class DecimalVar(ScriptVariable):
    """
    A fixed-precision decimal number.

    Value bounds, total digits, and decimal places are optional.
    """

    form_field = forms.DecimalField

    def __init__(self, min_value=None, max_value=None, max_digits=None, decimal_places=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Constraints are compared against None so that zero values like
        # decimal_places=0 stick
        if min_value is not None:
            self.field_attrs['min_value'] = min_value
        if max_value is not None:
            self.field_attrs['max_value'] = max_value
        if max_digits is not None:
            self.field_attrs['max_digits'] = max_digits
        if decimal_places is not None:
            self.field_attrs['decimal_places'] = decimal_places


class BooleanVar(ScriptVariable):
    """A true/false toggle, rendered as a checkbox."""

    form_field = forms.BooleanField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Django treats an unchecked required BooleanField as a validation error, so
        # the flag is always forced off
        self.field_attrs['required'] = False


class ChoiceVar(ScriptVariable):
    """
    A dropdown selecting exactly one of a static set of choices.

    Choices are given as (value, label) two-tuples:

        protocol = ChoiceVar(
            choices=(
                ('ssh', 'SSH'),
                ('telnet', 'Telnet'),
            )
        )
    """

    form_field = forms.ChoiceField

    def __init__(self, choices, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # A leading blank entry keeps the dropdown from silently preselecting the
        # first real choice
        self.field_attrs['choices'] = add_blank_choice(choices)


class MultiChoiceVar(ScriptVariable):
    """
    A multi-select over a static set of choices.

    No blank entry is added since an empty selection is already expressible.
    """

    form_field = forms.MultipleChoiceField

    def __init__(self, choices, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.field_attrs['choices'] = choices


class DateVar(ScriptVariable):
    """A calendar date, edited with NetBox's date picker."""

    form_field = forms.DateField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # The picker lives on this variable's field attributes, so an author-supplied
        # widget wins and the Django field class is never touched
        self.field_attrs.setdefault('widget', DatePicker())


class DateTimeVar(ScriptVariable):
    """A date plus a time of day, edited with NetBox's date-time picker."""

    form_field = forms.DateTimeField

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Same widget handling as DateVar
        self.field_attrs.setdefault('widget', DateTimePicker())


class ObjectVar(ScriptVariable):
    """
    A reference to a single NetBox object, chosen through a dynamic API-backed dropdown.

    :param model: The NetBox model to select from
    :param query_params: Extra query parameters for the dropdown's REST lookups
    :param context: Mapping of template context variables used when rendering the
        dropdown options
    :param null_option: Label offered for an explicit empty selection
    :param selector: Add the advanced object-selector widget
    :param quick_add: Add a widget for creating the related object on the spot
    """

    form_field = DynamicModelChoiceField

    def __init__(
        self,
        model,
        query_params=None,
        context=None,
        null_option=None,
        selector=False,
        quick_add=False,
        *args,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)

        self.field_attrs.update(
            {
                'queryset': model.objects.all(),
                'query_params': query_params,
                'context': context,
                'null_option': null_option,
                'selector': selector,
                'quick_add': quick_add,
            }
        )


class MultiObjectVar(ObjectVar):
    """The multi-select counterpart of ObjectVar, accepting one or more objects."""

    form_field = DynamicModelMultipleChoiceField


class FileVar(ScriptVariable):
    """A file uploaded through the run form."""

    form_field = forms.FileField


class IPAddressVar(ScriptVariable):
    """A bare IPv4 or IPv6 host address. Values carrying a mask are rejected."""

    form_field = IPAddressFormField


class IPAddressWithMaskVar(ScriptVariable):
    """An IPv4 or IPv6 address that must carry its prefix length, like 203.0.113.5/24."""

    form_field = IPNetworkFormField


class IPNetworkVar(ScriptVariable):
    """
    An IPv4 or IPv6 network.

    The value must be a base network address (no host bits set), and prefix-length bounds
    are optional.
    """

    form_field = IPNetworkFormField

    def __init__(self, min_prefix_length=None, max_prefix_length=None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.field_attrs['validators'] = [prefix_validator]
        if min_prefix_length is not None:
            self.field_attrs['validators'].append(MinPrefixLengthValidator(min_prefix_length))
        if max_prefix_length is not None:
            self.field_attrs['validators'].append(MaxPrefixLengthValidator(max_prefix_length))
