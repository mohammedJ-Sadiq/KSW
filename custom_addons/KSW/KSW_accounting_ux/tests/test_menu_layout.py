from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestMenuLayout(TransactionCase):
    """The Accounting app as an accountant actually receives it (load_menus)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.adviser = cls.env['res.users'].create({
            'name': 'Menu Adviser', 'login': 'ksw_menu_adviser',
            'group_ids': [(6, 0, [cls.env.ref('base.group_user').id,
                                  cls.env.ref('account.group_account_manager').id])],
        })
        cls.menus = cls.env['ir.ui.menu'].with_user(cls.adviser).load_menus(False)
        cls.root = cls.env.ref('account.menu_finance').id

    def _children(self, menu_id):
        return [self.menus[c] for c in self.menus[menu_id]['children']]

    def _names(self, menu_id):
        return [m['name'] for m in self._children(menu_id)]

    def test_top_level_sections_in_order(self):
        self.assertEqual(self._names(self.root), [
            'Dashboard', 'Customers', 'Vendors', 'Bank and Cash', 'Accounting',
            'Review', 'Reporting', 'Configuration'])

    def test_no_dropdown_mixes_items_and_headers(self):
        """A plain item next to section headers reads as belonging to one."""
        for section in self._children(self.root):
            kinds = {bool(c['children']) for c in self._children(section['id'])}
            if section['name'] == 'Configuration':
                # Settings first, then headers: the stock convention
                kinds = {bool(c['children']) for c in self._children(section['id'])
                         if c['name'] != 'Settings'}
            self.assertLessEqual(len(kinds), 1, f'{section["name"]} mixes items and headers')

    def test_no_empty_folder_and_no_duplicate_entry(self):
        seen = {}

        def walk(menu_id, path):
            for child in self._children(menu_id):
                here = f'{path} > {child["name"]}'
                if child['children']:
                    walk(child['id'], here)
                else:
                    action = (child['action_model'], child['action_id'])
                    self.assertTrue(child['action_id'], f'{here} has neither action nor children')
                    self.assertNotIn(action, seen, f'{here} duplicates {seen.get(action)}')
                    seen[action] = here
        walk(self.root, 'Accounting')

    def test_statements_and_audit_reports(self):
        reporting = self.env.ref('account.menu_finance_reports').id
        headers = {m['name']: m['id'] for m in self._children(reporting)}
        self.assertEqual(list(headers), [
            'Statement Reports', 'Audit Reports', 'Partner Reports',
            'Taxes & Fiscal', 'Management'])
        self.assertEqual(self._names(headers['Statement Reports']),
                         ['Balance Sheet', 'Profit and Loss', 'Cash Flow Statement'])
        self.assertEqual(self._names(headers['Audit Reports']),
                         ['General Ledger', 'Trial Balance', 'Journal Ledger'])
        self.assertIn('Statement of Account', self._names(headers['Partner Reports']))

    def test_vendor_oca_wrappers_hidden(self):
        """The OCA folders emptied by the layout must not reach the client."""
        for xmlid in ('account_asset_management.menu_finance_assets',
                      'account_usability.menu_accounting_bank_and_cash',
                      'mis_builder.mis_report_conf_menu',
                      'date_range_account.menu_date_range_root'):
            self.assertNotIn(self.env.ref(xmlid).id, self.menus, xmlid)
