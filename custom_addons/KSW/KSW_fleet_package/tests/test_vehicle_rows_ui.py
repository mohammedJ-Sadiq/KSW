"""The vehicles of a released employee are rows the server created, so the
web client never validated them: Submit came back as a bare server error with
no red field. Drive the real form and check the client stops it.

Needs the project venv: `browser_js` requires `websocket-client`, and without
it the test silently skips.
"""
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestVehicleRowsUI(HttpCase):

    def test_unanswered_vehicle_is_flagged_on_submit(self):
        department = self.env['hr.department'].create({'name': 'PK UI Dept', 'x_fleet_holder': True})
        driver = self.env['hr.employee'].create({'name': 'PK UI Driver', 'department_id': department.id})
        truck = self.env['ksw.fleet.vehicle'].create({'name': 'PK-UI-1', 'vehicle_type': 'isuzu',
                                                      'x_department_id': department.id})
        truck._ksw_set_custodian(driver)
        request = self.env['ksw.employee.request'].create({
            'request_type': 'release', 'department_id': department.id, 'reason': 'Route closed',
            'release_line_ids': [(0, 0, {'employee_id': driver.id})]})
        self.assertTrue(request.x_vehicle_ids)
        code = """
            (async () => {
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                const env = odoo.__WOWL_DEBUG__.root.env;
                await env.services.action.doAction({
                    type: 'ir.actions.act_window', res_model: 'ksw.employee.request',
                    res_id: %(id)d, views: [[false, 'form']],
                });
                let button;
                for (let i = 0; i < 40 && !button; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                    button = document.querySelector('button[name="action_submit"]');
                }
                button.click();
                await new Promise((r) => setTimeout(r, 1500));
                const active = document.querySelector('.o_notebook .nav-link.active');
                if (!active || !active.textContent.includes('Vehicles in Their Custody')) {
                    console.error('the form did not open the Vehicles in Their Custody tab');
                }
                if (!document.querySelector('.o_data_cell.o_invalid_cell[name="disposition"]')) {
                    console.error('the unanswered "If He Leaves" cell is not red');
                }
                if (!document.querySelector('.o_notebook .o_page_invalid')) {
                    console.error('the Vehicles in Their Custody tab is not flagged');
                }
                if (document.querySelector('.o_error_dialog, .modal .o_dialog_error')) {
                    console.error('the server was reached instead of the client stopping Submit');
                }
                console.log('test successful');
            })();
        """ % {'id': request.id}
        self.browser_js('/odoo', code, ready='odoo.isReady === true', login='admin', timeout=90)
        self.assertEqual(request.state, 'draft')

    def test_no_licence_message_offers_the_hr_request(self):
        department = self.env['hr.department'].create({'name': 'PK UI Dept 2', 'x_fleet_holder': True})
        driver = self.env['hr.employee'].create({'name': 'PK UI Leaver', 'department_id': department.id})
        newcomer = self.env['hr.employee'].create({'name': 'PK UI Newcomer', 'department_id': department.id})
        truck = self.env['ksw.fleet.vehicle'].create({'name': 'PK-UI-2', 'vehicle_type': 'isuzu',
                                                      'x_department_id': department.id})
        truck._ksw_set_custodian(driver)
        request = self.env['ksw.employee.request'].create({
            'request_type': 'release', 'department_id': department.id, 'reason': 'Route closed',
            'release_line_ids': [(0, 0, {'employee_id': driver.id})]})
        request.x_vehicle_ids.write({'disposition': 'driver_change', 'new_driver_id': newcomer.id})
        code = """
            (async () => {
                const wait = (ms) => new Promise((r) => setTimeout(r, ms));
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) { await wait(250); }
                const env = odoo.__WOWL_DEBUG__.root.env;
                await env.services.action.doAction({
                    type: 'ir.actions.act_window', res_model: 'ksw.employee.request',
                    res_id: %(id)d, views: [[false, 'form']],
                });
                let el;
                for (let i = 0; i < 40 && !el; i++) {
                    await wait(250);
                    el = document.querySelector('button[name="action_submit"]');
                }
                el.click();
                el = null;
                for (let i = 0; i < 40 && !el; i++) {
                    await wait(250);
                    el = [...document.querySelectorAll('.modal button')].find(
                        (b) => b.textContent.includes('Request It from HR'));
                }
                if (!el) { console.error('the licence message offers no HR request'); return; }
                el.click();
                for (let i = 0; i < 40; i++) {
                    await wait(250);
                    if (document.querySelector('.o_notification')) { break; }
                }
                if (!document.querySelector('.o_notification')?.textContent.includes('Requested from HR')) {
                    console.error('no confirmation that HR has the request');
                }
                console.log('test successful');
            })();
        """ % {'id': request.id}
        self.browser_js('/odoo', code, ready='odoo.isReady === true', login='admin', timeout=90)
        dr = request.x_vehicle_ids._licence_request(newcomer)
        self.assertEqual(dr.requested_by_id, self.env.ref('base.user_admin'))
