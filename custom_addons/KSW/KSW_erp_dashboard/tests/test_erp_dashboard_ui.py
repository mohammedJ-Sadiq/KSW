from odoo.tests import HttpCase, tagged


@tagged('post_install', '-at_install')
class TestErpDashboardUI(HttpCase):
    """Both landing pages render in a real browser, with their figures, and no JS error."""

    def _open(self, xmlid, marker):
        action = self.env.ref(xmlid)
        self.browser_js(
            f'/odoo/action-{action.id}',
            "console.log('test successful')",
            ready=f"document.querySelectorAll('.o_ksw_dashboard {marker}').length > 0",
            login='admin', timeout=120)

    def test_finance_dashboard_renders(self):
        self._open('KSW_erp_dashboard.action_ksw_finance_dashboard', '.o_ksw_chart canvas')

    def test_inventory_dashboard_renders(self):
        self._open('KSW_erp_dashboard.action_ksw_inventory_dashboard', '.o_ksw_tile')

    def test_menus_open_the_overview(self):
        self.assertEqual(self.env.ref('account.menu_board_journal_1').action,
                         self.env.ref('KSW_erp_dashboard.action_ksw_finance_dashboard'))
        self.assertEqual(self.env.ref('stock.stock_picking_type_menu').action,
                         self.env.ref('KSW_erp_dashboard.action_ksw_inventory_dashboard'))
