"""Browser test: ticking Cash Sale fills the client and the product.

The user reported an empty client and product on a cash dispatch in the
browser while every server-side check (Form, onchange) filled both. The cause
was client-side only: `boolean_toggle` defaults to autosave, so the click saved
the new record (which fails on the empty required client) instead of running
the onchange that fills it. Fixed with options="{'autosave': False}". This
drives the real form in Chrome as a dispatcher.

Needs the project venv (`websocket-client`):

    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 --test-enable \\
      --test-tags /KSW_water_delivery:TestWaterCashUI -u KSW_water_delivery \\
      --stop-after-init
"""
from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestWaterCashUI(HttpCase):

    def test_cash_sale_fills_client_and_product_in_the_browser(self):
        env = self.env
        desk = new_test_user(env, 'cash_ui_desk', groups='KSW_water_delivery.group_water_dispatch')
        branch = env['stock.picking.type'].create({
            'name': 'Cash UI Branch', 'code': 'outgoing', 'sequence_code': 'CUI',
            'warehouse_id': env.ref('stock.warehouse0').id,
            'x_is_water_delivery': True, 'x_branch_code': '977',
            'x_dispatcher_user_ids': [(6, 0, desk.ids)],
        })
        till = env['account.journal'].create({'name': 'UI Till', 'code': 'UITL', 'type': 'cash'})
        branch.write({'x_cash_journal_id': till.id})
        branch.action_create_cash_customer()
        product = env['product.product'].create({
            'name': 'UI Cash Water', 'type': 'consu', 'sale_ok': True})
        env['product.pricelist.item'].create({
            'x_client_id': branch.x_cash_partner_id.id, 'product_id': product.id,
            'fixed_price': 10.0})
        action = env.ref('KSW_water_delivery.action_water_dispatches')

        code = """
            (async () => {
                const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) { await wait(250); }
                const env = odoo.__WOWL_DEBUG__.root.env;
                await env.services.action.doAction(%(action)d, {viewType: 'form'});
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.o_form_view .o_field_widget[name=x_is_cash]')) { break; }
                    await wait(250);
                }
                await wait(500);
                const text = (name) => {
                    const el = document.querySelector('.o_form_view .o_field_widget[name=' + name + ']');
                    if (!el) { return null; }
                    const input = el.querySelector('input');
                    return (input ? input.value : el.textContent).trim();
                };
                console.log('KSW_BEFORE ' + JSON.stringify({branch: text('picking_type_id')}));
                const toggle = document.querySelector('.o_form_view .o_field_widget[name=x_is_cash] input');
                console.log('KSW_TOGGLE before=' + toggle.checked + ' html=' + toggle.outerHTML);
                toggle.click();
                await wait(3000);
                console.log('KSW_TOGGLE after=' + document.querySelector(
                    '.o_form_view .o_field_widget[name=x_is_cash] input').checked);
                const state = {
                    branch: text('picking_type_id'), client: text('partner_id'),
                    product: text('product_id'), amount: text('x_cash_amount'),
                    sellButton: [...document.querySelectorAll('.o_form_view .o_form_statusbar button')]
                        .some((b) => b.textContent.includes('Sell and Deliver Now')),
                    driverShown: !!document.querySelector('.o_form_view .o_field_widget[name=driver_user_id]'),
                };
                console.log('KSW_AFTER ' + JSON.stringify(state));
                // Discard: the harness refuses to finish on a dirty form.
                const discard = document.querySelector('.o_form_button_cancel');
                if (discard) { discard.click(); await wait(800); }
                if (!state.client || !state.product || state.amount.startsWith('0.00')
                        || !state.sellButton || state.driverShown) {
                    console.error('Cash Sale left the form empty: ' + JSON.stringify(state));
                } else {
                    console.log('test successful');
                }
            })();
        """ % {'action': action.id}
        self.browser_js('/odoo', code, login=desk.login, timeout=90)
