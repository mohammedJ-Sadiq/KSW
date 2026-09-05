"""Browser test: the Statement of Account's Reference/Description links.

Two defects a shell probe cannot see (Odoo 19 Pitfalls #40 — a dialog can
render "correctly" in `odoo shell` while the real client shows nothing,
or crashes):

1. A `<field>` with no `widget=` in a (non-editable) list view takes
   `list_renderer.js`'s "plain text" fast path (`canUseFormatter`) and
   never mounts the actual Many2One widget — the text shows, but never
   as a clickable `o_form_uri` link. `deduction_id` needs
   `widget="many2one"` explicitly to become a link at all.

2. A hand-built `ir.actions.act_window` dict with only `view_mode` (no
   `views`) works when a `type="object"` button triggers it (the button
   flow fills in the rest) but crashes `env.services.action.doAction()`
   called directly from a JS widget: `_preprocessAction` reads
   `action.views.map(...)`. `action_open_payslip` now returns
   `payslip.get_formview_action()`, which is always complete.
"""
from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestStatementUI(HttpCase):

    def test_reference_and_payslip_links_render_and_open(self):
        Line = self.env['ksw.deduction.line']
        line = Line.search(
            [('payslip_id', '!=', False), ('state', '=', 'paid')], limit=1)
        self.assertTrue(line, 'need one installment settled via payroll')
        employee = line.employee_id

        code = """
            (async () => {
                for (let i = 0; i < 60 && !odoo.__WOWL_DEBUG__; i++) {
                    await new Promise((r) => setTimeout(r, 250));
                }
                const env = odoo.__WOWL_DEBUG__.root.env;
                const wizardId = await env.services.orm.create(
                    'ksw.deduction.statement.wizard',
                    [{employee_id: %(employee_id)d}]
                );
                const action = await env.services.orm.call(
                    'ksw.deduction.statement.wizard', 'action_view',
                    [[wizardId[0]]]);
                // action_view() returns view_mode only — normal for a
                // type="object" button, but doAction() from raw JS
                // needs `views` filled in ourselves for THIS bootstrap
                // step (unrelated to the defect under test).
                action.views = [[false, 'list']];
                await env.services.action.doAction(action);
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.o_list_table tbody tr')) break;
                    await new Promise((r) => setTimeout(r, 250));
                }
                await new Promise((r) => setTimeout(r, 500));

                const refLink = document.querySelector(
                    'td[name="deduction_id"] a.o_form_uri');
                const paySlipLink = document.querySelector(
                    'td[name="label"] a.o_form_uri');
                if (!refLink) {
                    console.error('KSW_FAIL no o_form_uri link on the ' +
                        'Reference column — check widget="many2one"');
                }
                if (!paySlipLink) {
                    console.error('KSW_FAIL no o_form_uri link on the ' +
                        'Description column for a payroll-settled row');
                }

                paySlipLink.click();
                for (let i = 0; i < 40; i++) {
                    if (document.querySelector('.o_form_view')) break;
                    await new Promise((r) => setTimeout(r, 250));
                }
                await new Promise((r) => setTimeout(r, 300));
                if (!document.querySelector('.o_form_view')) {
                    console.error('KSW_FAIL clicking the payslip link did ' +
                        'not open a form (see action_open_payslip)');
                }
                console.log('test successful');
            })();
        """ % {'employee_id': employee.id}
        self.browser_js(
            '/odoo', code,
            ready='odoo.isReady === true',
            login='admin', timeout=90,
        )
