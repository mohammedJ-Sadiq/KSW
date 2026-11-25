# -*- coding: utf-8 -*-
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestPhoneMenu(HttpCase):
    """Phone menu: never empty, Home on top, an app's screens listed in place.

    Before (Nov 2026): on Home the menu was empty apart from two buttons,
    Home and All Apps, and picking an app loaded its first screen before the
    user could choose the one they wanted."""

    browser_size = '390x844'
    touch_enabled = True

    def test_menu_from_home_to_a_screen(self):
        self.env['res.users'].create({
            'name': 'KSW Phone Menu', 'login': 'ksw_phone_menu', 'password': 'ksw_phone_menu',
            'ksw_tour_done': True,
            'group_ids': [(6, 0, [
                self.env.ref('base.group_user').id,
                self.env.ref('base.group_partner_manager').id,
            ])],
        })
        self.browser_js('/odoo', """
            (async () => {
                const wait = (fn, what, ms = 15000) => new Promise((ok, ko) => {
                    const t0 = Date.now();
                    const h = setInterval(() => {
                        const v = fn();
                        if (v) { clearInterval(h); ok(v); }
                        else if (Date.now() - t0 > ms) { clearInterval(h); ko(new Error('timeout: ' + what)); }
                    }, 100);
                });
                const $ = (s) => document.querySelector(s);
                await wait(() => $('.ksw_home'), 'Home');
                (await wait(() => $('.o_main_navbar .o_menu_toggle'), 'menu button')).click();
                const panel = await wait(() => $('.o_app_menu_sidebar .ksw_panel'), 'panel');
                if (!$('.o_app_menu_sidebar .ksw_panel_home')) { console.error('no Home row'); return; }
                if ([...document.querySelectorAll('.o_app_menu_sidebar a')].some((a) => a.textContent.includes('All Apps'))) {
                    console.error('All Apps button still there'); return;
                }
                // On Home the menu lists the apps: never empty.
                const apps = panel.querySelectorAll('.ksw_panel_apps > li');
                if (!apps.length) { console.error('menu on Home is empty'); return; }
                // Picking an app with screens lists them in place, nothing opens.
                const withScreens = [...apps].find((li) => {
                    const menu = odoo.__WOWL_DEBUG__.root.env.services.menu;
                    const m = menu.getAll().find((x) => x.xmlid === li.dataset.menuXmlid);
                    return m && menu.getMenuAsTree(m.id).childrenTree.length;
                });
                if (!withScreens) { console.error('no app with screens for this user'); return; }
                withScreens.click();
                await wait(() => $('.o_app_menu_sidebar .ksw_panel_app'), 'app screens');
                if (!$('.ksw_home')) { console.error('picking an app left Home'); return; }
                // "Change app" goes back to the apps.
                $('.o_app_menu_sidebar .ksw_panel_app').click();
                await wait(() => $('.o_app_menu_sidebar .ksw_panel_apps'), 'apps again');
                // Home row closes the menu and stays on Home.
                $('.o_app_menu_sidebar .ksw_panel_home').click();
                await wait(() => !$('.o_app_menu_sidebar'), 'menu closed');
                console.log('test successful');
            })();
        """, ready='odoo.isReady', login='ksw_phone_menu', timeout=90)
