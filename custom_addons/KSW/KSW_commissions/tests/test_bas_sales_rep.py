"""BAS rep code -> employee mapping (ksw.bas.sales.rep) and the pull that
uses it.

BAS renames rep labels at will ("Abu Sadeq" became "Abu Sadeq-K"), which
silently broke the old match on the label text. The mapping is keyed on the
rep CODE, filled from BAS on every pull, and only the employee is picked by
a person — once.
"""
from odoo.tests.common import TransactionCase


class TestBasSalesRep(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        Emp = env['hr.employee'].sudo()
        cls.emp_alias = Emp.create({
            'name': 'Zyxw Rep Alias Person',
            'x_commission_import_name': 'Qwer Nick',
        })
        cls.emp_words = Emp.create({'name': 'Plmok Ijnuh Ygvtf'})
        cls.emp_manual = Emp.create({'name': 'Mnbv Manual Pick'})
        Cust = env['ksw.bas.customer'].sudo()

        def cust(code, s_code, s_name, c_code, c_name):
            return Cust.create({
                'bas_code': code, 'name_en': code,
                'seller_code': s_code, 'seller_name': s_name,
                'collector_code': c_code, 'collector_name': c_name,
            })

        cls.c_alias = cust('T9RC001', 'T901', 'Qwer Nick-K',
                           'T901', 'Qwer Nick-K')
        cls.c_words = cust('T9RC002', 'T902', 'Plmok Ygvtf',
                           'T903', 'Sales manager')
        cls.c_oneword = cust('T9RC003', 'T904', 'Plmok', 'T904', 'Plmok')

        cls.Rep = env['ksw.bas.sales.rep']
        cls.Rep._sync_from_bas_customers()

    def _rep(self, code):
        return self.Rep.search([('code', '=', code)])

    def test_rows_created_and_suggested(self):
        # "-K" marker stripped before the alias comparison
        self.assertEqual(self._rep('T901').employee_id, self.emp_alias)
        # every word of a two-word name in exactly one employee name
        self.assertEqual(self._rep('T902').employee_id, self.emp_words)
        # a house account is flagged, not guessed
        self.assertTrue(self._rep('T903').is_house)
        self.assertEqual(self._rep('T903').state, 'house')
        # a one-word nickname is never guessed
        self.assertFalse(self._rep('T904').employee_id)
        self.assertEqual(self._rep('T904').state, 'todo')

    def test_mapping_drives_customer_and_survives_rename(self):
        rep = self._rep('T904')
        rep.write({'employee_id': self.emp_manual.id})
        self.assertEqual(self.c_oneword.effective_sales_rep_id,
                         self.emp_manual)
        self.assertEqual(self.c_oneword.effective_collector_id,
                         self.emp_manual)
        # BAS renames the rep: label refreshed, link kept
        self.c_oneword.write({'seller_name': 'Plmok-K',
                              'collector_name': 'Plmok-K'})
        self.Rep._sync_from_bas_customers()
        self.c_oneword._recompute_effective_reps()
        self.assertEqual(rep.employee_id, self.emp_manual)
        self.assertEqual(self.c_oneword.effective_sales_rep_id,
                         self.emp_manual)

    def test_resync_never_overwrites_a_choice(self):
        rep = self._rep('T901')
        rep.write({'employee_id': self.emp_manual.id})
        self.Rep._sync_from_bas_customers()
        self.assertEqual(rep.employee_id, self.emp_manual)

    def test_house_rep_credits_nobody(self):
        self.c_words._recompute_effective_reps()
        self.assertEqual(self.c_words.effective_sales_rep_id, self.emp_words)
        self.assertFalse(self.c_words.effective_collector_id)

    def test_pull_attributes_by_code(self):
        env = self.env
        env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.sc_ledger_prefixes', '*')
        Ledger = env['ksw.bas.ledger.line'].sudo()
        Ledger.create({
            'bas_key': 'T9RC-INV-1', 'ftype': '002', 'date': '2031-02-28',
            'debit_account': 'T9RC003', 'amount': 1150.0,
            'seller_code': 'T904'})
        Ledger.create({
            'bas_key': 'T9RC-PAY-1', 'ftype': '018', 'date': '2031-02-11',
            'credit_account': 'T9RC003', 'amount': 460.0,
            'seller_code': 'T904'})
        self._rep('T904').write({'employee_id': self.emp_manual.id})
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2031-02-01'})
        sheet.action_pull_from_bas()
        line = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.emp_manual)
        # VAT (15%) removed
        self.assertAlmostEqual(line.achieved_sales, 1000.0, 2)
        self.assertAlmostEqual(line.achieved_collection, 400.0, 2)
