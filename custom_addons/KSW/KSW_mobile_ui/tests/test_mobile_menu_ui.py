# -*- coding: utf-8 -*-
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestMobileMenuUI(HttpCase):
    """The phone app menu: a heading that holds its own pages
    (Accounting > Accounting > Management) must stack its pages under it.
    `display: flex` on every nested <li> once laid them out side by side."""

    browser_size = '390x844'
    touch_enabled = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = cls.env['res.users'].create({
            'name': 'KSW Mobile Menu', 'login': 'ksw_mobile_menu',
            'password': 'ksw_mobile_menu',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('account.group_account_manager').id,
            ])],
        })

    def test_nested_heading_stacks_its_pages(self):
        Menu = self.env['ir.ui.menu']
        app = self.env.ref('account.menu_finance')
        # The first heading inside a section that has pages of its own.
        heading = Menu.search([
            ('parent_id.parent_id', '=', app.id), ('child_id', '!=', False),
        ], limit=1)
        self.assertTrue(heading, 'no nested heading under Accounting')
        self.browser_js('/odoo', """
            (async () => {
                const wait = (fn, what, ms = 15000) => new Promise((ok, ko) => {
                    const t0 = Date.now();
                    const h = setInterval(() => {
                        const v = fn();
                        if (v) { clearInterval(h); ok(v); }
                        else if (Date.now() - t0 > ms) {
                            clearInterval(h); ko(new Error('timeout: ' + what + ' | sidebar: ' + (document.querySelector('.o_app_menu_sidebar')?.innerText || 'none').slice(0, 600)));
                        }
                    }, 100);
                });
                const toggle = () => wait(() => document.querySelector(
                    '.o_main_navbar .o_menu_toggle'), 'menu button');
                // Pick the app as a user would. With KSW_backend_theme the menu
                // opens on the apps and lists the picked app's pages in place;
                // core needs All Apps, then the app, then the menu again.
                (await toggle()).click();
                await wait(() => document.querySelector('.o_app_menu_sidebar'), 'sidebar');
                const themed = document.querySelector('.o_app_menu_sidebar .ksw_panel');
                if (!themed) {
                    (await wait(() => document.querySelector(
                        '.o_app_menu_sidebar .o_sidebar_topbar a'), 'All Apps')).click();
                }
                (await wait(() => document.querySelector(
                    '.o_app_menu_sidebar li[data-menu-xmlid="account.menu_finance"]'),
                    'Accounting app')).click();
                if (!themed) {
                    await wait(() => !document.querySelector('.o_app_menu_sidebar'),
                        'sidebar closed');
                    (await toggle()).click();
                }
                const title = await wait(() => document.querySelector(
                    '.o_app_menu_sidebar div[data-menu-xmlid="%(xmlid)s"]'), 'heading');
                // Measure once the sidebar has finished sliding in.
                await wait(() => document.querySelector('.o_app_menu_sidebar')
                    .getBoundingClientRect().left >= 0, 'sidebar open');
                await new Promise((ok) => setTimeout(ok, 300));
                title.scrollIntoView({block: 'start'});
                const list = title.parentElement.querySelector(':scope > ul');
                const t = title.getBoundingClientRect();
                const l = list.getBoundingClientRect();
                const page = list.querySelector('li').getBoundingClientRect();
                const side = document.querySelector(
                    '.o_app_menu_sidebar').getBoundingClientRect();
                if (l.top < t.bottom - 1) {
                    console.error(`pages beside the heading: heading ${t.left},${t.top}-${t.right},${t.bottom}; pages ${l.left},${l.top}-${l.right},${l.bottom}`);
                } else if (page.width < side.width * 0.6) {
                    console.error(`page row too narrow: ${page.width} of ${side.width}`);
                } else {
                    console.log('test successful');
                }
            })();
        """ % {'xmlid': heading.get_external_id()[heading.id]},
            ready='odoo.isReady', login='ksw_mobile_menu', timeout=90)
