from django.test import TestCase

from netbox_custom_scripts.forms import MigrationCutoverForm


class MigrationCutoverFormTestCase(TestCase):
    """The one acknowledgement standing between an operator and the irreversible pass."""

    def test_an_unticked_box_is_refused(self):
        form = MigrationCutoverForm({'confirm': 'true'})

        self.assertFalse(form.is_valid())
        self.assertIn('backup_taken', form.errors)

    def test_a_ticked_box_is_accepted(self):
        self.assertTrue(MigrationCutoverForm({'confirm': 'true', 'backup_taken': 'on'}).is_valid())

    def test_the_box_is_visible_so_the_page_can_render_it(self):
        # ConfirmationForm's own fields are hidden, and the confirmation template renders only
        # those, so a hidden acknowledgement would be submitted without ever being seen.
        self.assertNotIn(MigrationCutoverForm()['backup_taken'], MigrationCutoverForm().hidden_fields())
