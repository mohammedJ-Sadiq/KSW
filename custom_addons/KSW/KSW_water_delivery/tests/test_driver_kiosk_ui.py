from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install', '-standard', 'ksw_kiosk_ui')
class TestDriverKioskUI(HttpCase):
    """Drive the real web client, because this is a client-side behaviour and
    nothing else can tell you whether it happens.

    The project has learned this twice over: a shell probe supplies the values
    the browser omits, and `new()` fires no onchange. A body class added by an
    OWL service is exactly the same kind of claim -- unprovable from Python.

    Needs `websocket-client`, which lives only in the project `.venv`; with
    `odoo19env` the test SILENTLY SKIPS and prints "0 failed"
    ([[Odoo 19 Pitfalls]] #40):

        .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 \
          --test-enable --test-tags /KSW_water_delivery:TestDriverKioskUI \
          -u KSW_water_delivery --stop-after-init
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.driver_user = cls.env['res.users'].create({
            'name': 'Kiosk Test Driver',
            'login': 'kiosk.driver',
            'password': 'kiosk.driver',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_driver').id,
            ])],
        })
        cls.env['hr.employee'].create({
            'name': 'Kiosk Test Driver', 'user_id': cls.driver_user.id,
        })
        cls.wide_user = cls.env['res.users'].create({
            'name': 'Kiosk Test Billing',
            'login': 'kiosk.billing',
            'password': 'kiosk.billing',
            'group_ids': [(6, 0, [
                cls.env.ref('KSW_water_delivery.group_water_driver').id,
                cls.env.ref('KSW_water_delivery.group_water_billing').id,
            ])],
        })
        cls.env['hr.employee'].create({
            'name': 'Kiosk Test Billing', 'user_id': cls.wide_user.id,
        })
        cls.action = cls.env.ref('KSW_water_delivery.action_water_delivery_my_notes')

    def _assert_kiosk(self, login, expected, message):
        """Open the app and report whether the kiosk class landed on <body>."""
        code = """
            (async () => {
                // The service runs at web client start; give it a moment.
                await new Promise((r) => setTimeout(r, 1500));
                const locked = document.body.classList.contains('o_ksw_driver_kiosk');
                const menu = document.querySelector('.o_navbar_apps_menu');
                const menuShown = !!menu && menu.offsetParent !== null;
                if (locked !== %(expected)s) {
                    console.error('kiosk class = ' + locked + ', expected %(expected)s');
                } else if (locked && menuShown) {
                    console.error('kiosk class set but the apps menu is still visible');
                } else {
                    console.log('test successful');
                }
            })();
        """ % {'expected': 'true' if expected else 'false'}
        self.browser_js(
            url_path='/odoo/action-%s' % self.action.id,
            code=code,
            login=login,
            timeout=90,
        )

    def test_a_driver_only_user_gets_the_single_app_screen(self):
        self._assert_kiosk(
            'kiosk.driver', True,
            'a pure driver must never see the app switcher',
        )

    def test_a_billing_user_keeps_the_rest_of_odoo_in_a_browser_tab(self):
        """The WHERE trigger is what locks this user, and it only fires inside
        the installed app. In an ordinary tab he must keep everything."""
        self._assert_kiosk(
            'kiosk.billing', False,
            'a billing user in a normal tab keeps the whole of Odoo',
        )
