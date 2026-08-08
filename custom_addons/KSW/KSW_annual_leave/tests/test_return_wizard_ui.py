"""Browser test: the Return-to-Approver dialog renders its options.

This exists because three server-side "verifications" of this dialog were
wrong. `fields_get()`, `onchange()`, `get_views()` and a `new()` record all
returned the right answer from `odoo shell`, while the real dialog showed
first the wrong options and then none at all.

The defect they all missed: `leave_id` was not in the wizard's form arch, so
the client never asked for it, `default_leave_id` never landed on the client's
record, and every leave-dependent compute ran against an empty leave. Each
shell probe passed `leave_id` explicitly and so could never reproduce it.

Needs the project venv — `browser_js` requires `websocket-client`, which only
`.venv` has:

    .venv/bin/python odoo-bin -c KSW_dev.conf --http-port=18077 --test-enable \\
      --test-tags /KSW_annual_leave:TestReturnWizardUI -u KSW_annual_leave \\
      --stop-after-init
"""
from datetime import date, timedelta

from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestReturnWizardUI(HttpCase):

    def _leave_at(self, chain_state):
        employee = self.env['hr.employee'].create({'name': 'UI Return Employee'})
        leave_type = self.env['hr.leave.type'].create({
            'name': 'Annual Leave UI Return Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        start = date.today() + timedelta(days=30)
        leave = self.env['hr.leave'].create({
            'employee_id': employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=3),
        })
        leave.write({'x_annual_approval_state': chain_state})
        return leave

    def test_radio_renders_the_reachable_steps(self):
        """A request at GM Final must offer the four steps behind it.

        The JS raises (failing the test) when the radio comes up empty, which
        is exactly the state the user reported.
        """
        leave = self._leave_at('pending_gm_final')
        self.assertTrue(self.env.ref('base.user_admin').has_group(
            'base.group_system'), 'the test drives the client as the admin')

        code = """
            (async () => {
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                const env = odoo.__WOWL_DEBUG__.root.env;
                const action = await env.services.orm.call(
                    'hr.leave', 'action_open_gm_return_wizard', [[%(leave_id)d]]);
                // A raw act_window dict from orm.call carries no `views` key.
                action.views = [[false, 'form']];
                await env.services.action.doAction(action);
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.modal .o_field_radio')) { break; }
                    await new Promise((r) => setTimeout(r, 250));
                }
                await new Promise((r) => setTimeout(r, 800));
                const labels = [...new Set([...document.querySelectorAll(
                    '.modal .o_radio_item')].map((el) => el.textContent.trim()))];
                console.log('KSW_RADIO ' + JSON.stringify(labels));
                const expected = ['Direct Manager', 'HR Approver',
                                  'GM (Initial Review)', 'Accounting'];
                if (JSON.stringify(labels) !== JSON.stringify(expected)) {
                    console.error('Return To offered ' + JSON.stringify(labels) +
                                  ' instead of ' + JSON.stringify(expected));
                }
                console.log('test successful');
            })();
        """ % {'leave_id': leave.id}
        self.browser_js(
            '/odoo/time-off', code,
            ready='odoo.isReady === true',
            login='admin', timeout=90,
        )

    def test_leave_id_is_in_the_form_arch(self):
        """Guard the actual defect.

        `default_leave_id` only reaches the client's record for fields the
        view asks for. Drop `leave_id` from the arch again and the dialog goes
        blank — silently, with every server-side check still passing.
        """
        arch = self.env.ref(
            'KSW_annual_leave.view_gm_return_approver_wizard_form').arch
        self.assertIn('name="leave_id"', arch)
        self.assertIn('name="allowed_step_ids"', arch)
