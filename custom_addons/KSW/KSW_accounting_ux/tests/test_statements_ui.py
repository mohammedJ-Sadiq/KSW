from odoo.tests import HttpCase, tagged

# Click the dialog's Show button, then wait for the MIS table.
MIS_DIALOG_JS = """
    const t0 = Date.now();
    let clicked = false;
    const tick = () => {
        const btn = document.querySelector('.modal button[name="action_open"]');
        if (!clicked && btn) { btn.click(); clicked = true; }
        if (document.querySelector('.oe_mis_builder_content table td')) {
            console.log('test successful');
        } else if (Date.now() - t0 > 20000) {
            console.error('MIS table never rendered (dialog clicked: ' + clicked + ')');
        } else { setTimeout(tick, 300); }
    };
    tick();
"""


@tagged('post_install', '-at_install')
class TestStatementsUi(HttpCase):
    def test_balance_sheet_menu_opens_report(self):
        self.browser_js('/odoo/action-KSW_accounting_ux.action_ksw_bs_wizard',
                        MIS_DIALOG_JS, login='admin', timeout=60)

    def test_cash_flow_menu_opens_report(self):
        self.browser_js('/odoo/action-KSW_accounting_ux.action_ksw_cf_wizard',
                        MIS_DIALOG_JS, login='admin', timeout=60)

    def test_statement_of_account_dialog_has_partner_picker(self):
        code = """
            const t0 = Date.now();
            const tick = () => {
                const field = document.querySelector('.modal div[name="partner_ids"]');
                if (field && document.querySelector('.modal button[name="button_export_html"]')) {
                    console.log('test successful');
                } else if (Date.now() - t0 > 20000) {
                    console.error('statement dialog or partner field missing');
                } else { setTimeout(tick, 300); }
            };
            tick();
        """
        self.browser_js('/odoo/action-KSW_accounting_ux.action_ksw_activity_statement',
                        code, login='admin', timeout=60)

    def test_bank_reconciliation_opens_without_a_journal(self):
        """The OCA view behind it is normally opened from one journal's card."""
        code = """
            const t0 = Date.now();
            const tick = () => {
                if (document.querySelector('.o_kanban_renderer, .o_view_nocontent')) {
                    console.log('test successful');
                } else if (Date.now() - t0 > 20000) {
                    console.error('bank reconciliation view never rendered');
                } else { setTimeout(tick, 300); }
            };
            tick();
        """
        self.browser_js('/odoo/action-KSW_accounting_ux.action_ksw_bank_reconcile',
                        code, login='admin', timeout=60)
