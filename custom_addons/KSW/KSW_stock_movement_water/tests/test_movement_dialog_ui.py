"""Browser test: the delivery number on a report row opens it in a pop-up that expands.

Needs the project venv (`websocket-client`):

    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 --test-enable \\
      --test-tags /KSW_stock_movement_water:TestMovementDialogUI --stop-after-init
"""
from odoo.tests import HttpCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestMovementDialogUI(HttpCase):

    def test_the_delivery_number_opens_an_expandable_pop_up(self):
        env = self.env
        user = new_test_user(env, 'sm_dialog_ui', groups='stock.group_stock_user')
        stock = env.ref('stock.stock_location_stock')
        customers = env.ref('stock.stock_location_customers')
        product = env['product.product'].create({'name': 'SM Dialog Water', 'type': 'consu'})
        picking = env['stock.picking'].create({
            'picking_type_id': env.ref('stock.picking_type_out').id,
            'location_id': stock.id, 'location_dest_id': customers.id,
            'move_ids': [(0, 0, {'product_id': product.id, 'product_uom_qty': 2.0,
                                 'location_id': stock.id, 'location_dest_id': customers.id})],
        })
        picking.action_confirm()
        picking.move_ids.write({'quantity': 2.0, 'picked': True})
        picking.button_validate()
        wizard = env['ksw.stock.movement.wizard'].create({
            'product_ids': [(6, 0, product.ids)], 'layout': 'flat'})
        action = wizard.with_user(user).action_show()

        code = """
            (async () => {
                const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) { await wait(250); }
                const env = odoo.__WOWL_DEBUG__.root.env;
                await env.services.action.doAction(%(action)s);
                const link = () => document.querySelector(
                    '.o_list_view .o_field_widget[name=reference] a.o_ksw_record_link');
                for (let i = 0; i < 40 && !link(); i++) { await wait(250); }
                if (!link()) { console.error('The delivery number is not a link'); return; }
                link().click();
                for (let i = 0; i < 40 && !document.querySelector('.modal .o_form_view'); i++) { await wait(250); }
                const modal = document.querySelector('.modal');
                const title = modal && modal.querySelector('.modal-title').textContent.trim();
                const expand = modal && modal.querySelector('.o_expand_button');
                console.log('KSW_DIALOG ' + JSON.stringify({open: !!modal, title, expand: !!expand}));
                if (!modal || !expand || !title.includes(%(name)s)) {
                    console.error('No expandable pop-up for the delivery');
                    return;
                }
                expand.click();
                for (let i = 0; i < 40 && document.querySelector('.modal'); i++) { await wait(250); }
                const fullPage = !document.querySelector('.modal') && document.querySelector('.o_form_view');
                console.log('KSW_EXPANDED ' + !!fullPage);
                if (!fullPage) { console.error('Expand did not open the full page'); return; }
                console.log('test successful');
            })();
        """ % {'action': __import__('json').dumps(action), 'name': __import__('json').dumps(picking.name)}
        self.browser_js('/odoo', code, login=user.login, timeout=90)
