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

    def _open_hub(self, xmlid, ready):
        dashboard = self.env.ref(xmlid)
        self.browser_js(f'/odoo/dashboards?dashboard_id={dashboard.id}', "console.log('test successful')",
                        ready=ready, login='admin', timeout=120)

    def test_overviews_open_in_the_dashboards_app(self):
        for xmlid in ('KSW_erp_dashboard.hub_dashboard_finance', 'KSW_erp_dashboard.hub_dashboard_inventory'):
            self._open_hub(xmlid, "document.querySelectorAll('.o_spreadsheet_dashboard_action .o_ksw_hub_page "
                                  ".o_ksw_chart canvas').length > 0")

    def test_spreadsheet_dashboards_still_render(self):
        # The patch only takes over entries that name an overview page.
        self._open_hub('spreadsheet_dashboard_account.dashboard_invoicing',
                       "document.querySelectorAll('.o_spreadsheet_dashboard_action .o-spreadsheet').length > 0"
                       " && !document.querySelector('.o_ksw_hub_page')")

