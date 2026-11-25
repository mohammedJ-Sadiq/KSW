# -*- coding: utf-8 -*-
from odoo.tests import HttpCase, TransactionCase, tagged


class TestTableScreensOpenOnList(TransactionCase):
    """A screen the phone user switched to Table opens on its list
    (ir.actions.act_window._get_action_dict reads the client's context)."""

    def setUp(self):
        super().setUp()
        self.action = self.env.ref('base.action_partner_form')

    def test_listed_action_opens_on_list(self):
        result = self.action.with_context(
            ksw_m_table_actions=[self.action.id])._get_action_dict()
        self.assertEqual(result['mobile_view_mode'], 'list')

    def test_other_actions_keep_their_phone_view(self):
        expected = self.action.mobile_view_mode
        self.assertEqual(self.action._get_action_dict()['mobile_view_mode'], expected)
        result = self.action.with_context(
            ksw_m_table_actions=[self.action.id + 100000])._get_action_dict()
        self.assertEqual(result['mobile_view_mode'], expected)

    def test_action_without_list_is_untouched(self):
        action = self.env['ir.actions.act_window'].create({
            'name': 'Kanban only', 'res_model': 'res.partner', 'view_mode': 'kanban,form',
        })
        result = action.with_context(ksw_m_table_actions=[action.id])._get_action_dict()
        self.assertEqual(result['mobile_view_mode'], 'kanban')


@tagged('post_install', '-at_install')
class TestLayoutToggleUI(HttpCase):
    """Phone: one Cards / Table button; Table is a one-line-per-record grid
    that stays inside the screen width, and the screen reopens as a table."""

    browser_size = '390x844'
    touch_enabled = True

    def test_cards_table_toggle(self):
        self.env['res.users'].create({
            'name': 'KSW Layout', 'login': 'ksw_layout', 'password': 'ksw_layout',
            'group_ids': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        self.env['res.partner'].create([{'name': 'Layout %s' % i} for i in range(30)])
        action = self.env.ref('base.action_partner_form')
        self.browser_js('/odoo/action-%s' % action.id, """
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
                await wait(() => $('.o_kanban_view'), 'opens on cards');
                (await wait(() => $('.ksw_m_layout_toggle'), 'toggle')).click();
                const table = await wait(() => $('.o_list_view .ksw_m_table'), 'table');
                const rows = table.querySelectorAll('tr.o_data_row');
                const rowH = rows[0].getBoundingClientRect().height;
                if (rowH > 60) { console.error('row too tall: ' + rowH); return; }
                if (document.documentElement.scrollWidth > window.innerWidth + 1) {
                    console.error('page scrolls sideways: ' + document.documentElement.scrollWidth); return;
                }
                const head = table.querySelector('thead th:not(.o_list_record_selector)');
                if (getComputedStyle(head).position !== 'sticky') { console.error('header not pinned'); return; }
                // Reopening the screen lands on the table straight away.
                await odoo.__WOWL_DEBUG__.root.env.services.action.doAction(%(action)d, {clearBreadcrumbs: true});
                const reopened = await wait(() => $('.o_kanban_view') || ($('.ksw_m_table') !== table && $('.ksw_m_table')), 'reopened');
                if (!reopened.classList.contains('ksw_m_table')) { console.error('reopened as cards'); return; }
                (await wait(() => $('.ksw_m_layout_toggle'), 'toggle back')).click();
                await wait(() => $('.o_kanban_view'), 'back to cards');
                console.log('test successful');
            })();
        """ % {'action': action.id}, ready='odoo.isReady', login='ksw_layout', timeout=90)
