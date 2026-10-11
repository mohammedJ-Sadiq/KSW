"""The 📍 button has to be ON THE SCREEN -- the failure that prompted it was a
driver on the Odoo form refused with no button to press. Only a real browser
can say whether a widget renders; a Python test calls the method directly.

Needs `websocket-client` (project `.venv` only; with `odoo19env` it silently
skips -- [[Odoo 19 Pitfalls]] #40).
"""
from odoo.tests import HttpCase, tagged

from .test_water_delivery import WaterDeliveryCommon


@tagged('post_install', '-at_install')
class TestClientPinButtonUI(WaterDeliveryCommon, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.picking_type.x_location_rule = 'enforce'
        cls.driver_user.password = 'pin.ui.driver'
        cls.driver_user.flush_recordset()

    def _open_form_and_check(self, check):
        code = """
            (async () => {
                const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                await wait(1000);
                const env = odoo.__WOWL_DEBUG__.root.env;
                await env.services.action.doAction(
                    'KSW_water_delivery.action_water_delivery_wizard',
                    { additionalContext: { default_partner_id: %(partner)d } });
                await wait(2500);
                const button = [...document.querySelectorAll('.o_ksw_client_pin button')]
                    .find((b) => b.textContent.includes('Send my location'));
                const issue = [...document.querySelectorAll('.modal-footer button')]
                    .find((b) => b.textContent.includes('Issue Delivery Note'));
                %(check)s
            })();
        """ % {'partner': self.customer.id, 'check': check}
        self.browser_js(url_path='/odoo', code=code, login=self.driver_user.login,
                        timeout=90)

    def test_the_button_shows_for_an_unlocated_client(self):
        self._open_form_and_check("""
            if (!button) { console.error('no Send my location button'); }
            else if (issue) { console.error('Issue button shown for an unlocated client'); }
            else { console.log('test successful'); }
        """)

    def test_no_button_once_the_client_is_located(self):
        self.customer.write({'partner_latitude': 26.42, 'partner_longitude': 50.08})
        self._open_form_and_check("""
            if (button) { console.error('button shown for a located client'); }
            else if (!issue) { console.error('Issue button missing for a located client'); }
            else { console.log('test successful'); }
        """)
