"""Sales & collection figures read from the BAS customer ledger
(ksw.sales.commission.bas), the way the accountant's workbook builds them.
"""
from odoo.tests.common import TransactionCase


class TestSalesCommissionBas(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        param = env['ir.config_parameter'].sudo()
        param.set_param('ksw_commissions.sc_ledger_prefixes', 'T8')
        Emp = env['hr.employee'].sudo()
        cls.march_rep = Emp.create({'name': 'Qazx March Rep'})
        cls.today_rep = Emp.create({'name': 'Qazx Today Rep'})
        cls.collector = Emp.create({'name': 'Qazx Collector'})
        Rep = env['ksw.bas.sales.rep'].sudo()
        for code, emp in (('T8A', cls.march_rep), ('T8B', cls.today_rep),
                          ('T8C', cls.collector)):
            Rep.create({'code': code, 'bas_name': code,
                        'employee_id': emp.id})
        Rep.create({'code': 'T8H', 'bas_name': 'Sales manager',
                    'is_house': True})
        # Customer now belongs to T8B; in March BAS stamped T8A on the
        # invoice. 60-day term, owes 1000 from last year.
        cls.customer = env['ksw.bas.customer'].sudo().create({
            'bas_code': 'T8001', 'name_en': 'T8 Customer',
            'seller_code': 'T8B', 'collector_code': 'T8C',
            'invoice_term_days': 60, 'credit_term_days': 150,
            'opening_balance': 1000.0,
        })
        L = env['ksw.bas.ledger.line'].sudo()

        def line(key, ftype, day, amount, seller, debit=True,
                 account='T8001'):
            L.create({
                'bas_key': key, 'ftype': ftype, 'date': day,
                'debit_account': account if debit else '',
                'credit_account': '' if debit else account,
                'amount': amount, 'seller_code': seller})

        # Month-end invoices Jan, Feb, Mar (VAT incl.)
        line('T8-I1', '002', '2030-01-31', 230.0, 'T8A')
        line('T8-I2', '002', '2030-02-28', 345.0, 'T8A')
        line('T8-I3', '002', '2030-03-31', 1150.0, 'T8A')
        # Receipts: one in Feb, one in March; a reversed March receipt
        line('T8-R1', '018', '2030-02-10', 500.0, 'T8C', debit=False)
        line('T8-R2', '018', '2030-03-15', 690.0, 'T8C', debit=False)
        line('T8-R3', '018', '2030-03-16', 115.0, 'T8C', debit=False)
        line('T8-R3X', '018', '2030-03-17', 115.0, 'T8C', debit=True)
        # Out of scope ledger: ignored everywhere
        line('T8-X1', '002', '2030-03-31', 9999.0, 'T8A', account='X9001')
        # A daily delivery ticket (600) is not a sale
        line('T8-D1', '600', '2030-03-05', 50.0, 'T8A')
        cls.Engine = env['ksw.sales.commission.bas']

    def _by(self, rows, field='employee'):
        out = {}
        for r in rows:
            out[r[field]] = out.get(r[field], 0.0) + r['net']
        return out

    def test_sales_by_rep_on_document_net_of_vat(self):
        f = self.Engine._period_figures('2030-03-01')
        sales = self._by(f['sales'])
        self.assertAlmostEqual(sales.get(self.march_rep), 1000.0, 2)
        self.assertNotIn(self.today_rep, sales)

    def test_sales_by_customer_current_rep_when_configured(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.sc_attribution', 'customer')
        f = self.Engine._period_figures('2030-03-01')
        sales = self._by(f['sales'])
        self.assertAlmostEqual(sales.get(self.today_rep), 1000.0, 2)

    def test_vat_rate_is_a_setting(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.sc_vat_rate', '0')
        f = self.Engine._period_figures('2030-03-01')
        self.assertAlmostEqual(self._by(f['sales'])[self.march_rep],
                               1150.0, 2)

    def test_collections_net_of_reversal(self):
        f = self.Engine._period_figures('2030-03-01')
        coll = self._by(f['collections'])
        self.assertAlmostEqual(coll.get(self.collector), 600.0, 2)

    def test_target_is_balance_less_invoices_within_term(self):
        # End of Feb balance: 1000 + 230 + 345 - 500 = 1075.
        # 60-day term = 2 months: Jan and Feb invoices not yet due.
        # Target = 1075 - 230 - 345 = 500 (VAT incl.) -> 434.78 net.
        f = self.Engine._period_figures('2030-03-01')
        row = [t for t in f['targets'] if t['account'] == 'T8001'][0]
        self.assertAlmostEqual(row['balance'], 1075.0, 2)
        self.assertAlmostEqual(row['gross'], 500.0, 2)
        self.assertEqual(row['employee'], self.collector)
        self.assertAlmostEqual(
            self.Engine._employee_collection_target(
                self.collector, '2030-03-01'), 500.0 / 1.15, 2)

    def test_term_source_is_a_setting(self):
        # LIMT_DAYS 150 = 5 months: every invoice still within term.
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_commissions.sc_term_source', 'limt_days')
        f = self.Engine._period_figures('2030-03-01')
        row = [t for t in f['targets'] if t['account'] == 'T8001'][0]
        self.assertAlmostEqual(row['gross'], 500.0, 2)  # 1075-575, floor
        self.assertEqual(row['term_days'], 150)

    def test_out_of_scope_and_delivery_tickets_ignored(self):
        f = self.Engine._period_figures('2030-03-01')
        accounts = {r['account'] for r in f['sales']}
        self.assertEqual(accounts, {'T8001'})
        self.assertAlmostEqual(sum(r['gross'] for r in f['sales']),
                               1150.0, 2)

    def test_pull_writes_detail_and_exclusion(self):
        env = self.env
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.march_rep.id, 'year': 2030,
            'role': 'sales', 'x_sales_excluded_accounts': 'T8001'})
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        line = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.march_rep)
        self.assertAlmostEqual(line.achieved_sales, 0.0, 2)
        det = sheet.x_bas_detail_ids.filtered(
            lambda d: d.kind == 'sales' and d.account == 'T8001')
        self.assertEqual(det.status, 'excluded')
        coll = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.collector)
        self.assertAlmostEqual(coll.target_collection, 500.0 / 1.15, 2)
        self.assertAlmostEqual(coll.achieved_collection, 600.0, 2)

    def test_target_adjustment_survives_pull(self):
        env = self.env
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        coll = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.collector)
        coll.x_target_collection_adjustment = -100.0
        sheet.action_pull_from_bas()
        self.assertAlmostEqual(coll.target_collection,
                               500.0 / 1.15 - 100.0, 2)

    def test_salesman_on_a_receipt_credits_the_customers_collector(self):
        # The salesman carried a cheque in: BAS stamps his code on the
        # receipt, but he is a salesman, so it belongs to the collector.
        env = self.env
        env['ksw.bas.ledger.line'].sudo().create({
            'bas_key': 'T8-R9', 'ftype': '015', 'date': '2030-03-20',
            'credit_account': 'T8001', 'amount': 230.0,
            'seller_code': 'T8A'})
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.march_rep.id, 'year': 2030, 'role': 'sales'})
        f = self.Engine._period_figures('2030-03-01')
        coll = self._by(f['collections'])
        self.assertNotIn(self.march_rep, coll)
        self.assertAlmostEqual(coll.get(self.collector), 600.0 + 200.0, 2)
        # and the target follows the collector, not the last receipt
        row = [t for t in f['targets'] if t['account'] == 'T8001'][0]
        self.assertEqual(row['employee'], self.collector)
        # sales are never moved by role: a collector's sales stay his
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.march_rep.id, 'year': 2031,
            'role': 'collect'})
        sales = self._by(self.Engine._period_figures('2030-03-01')['sales'])
        self.assertIn(self.march_rep, sales)

    def test_repull_clears_figures_no_longer_earned(self):
        env = self.env
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        coll = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.collector)
        self.assertTrue(coll.achieved_collection)
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.collector.id, 'year': 2030, 'role': 'sales'})
        sheet.action_pull_from_bas()
        self.assertEqual(coll.achieved_collection, 0.0)
        self.assertEqual(coll.target_collection, 0.0)

    def test_pull_refreshes_role_and_sales_target_from_new_profile(self):
        env = self.env
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        line = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.march_rep)
        self.assertEqual(line.target_sales, 0.0)
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.march_rep.id, 'year': 2030, 'role': 'both',
            'annual_sales_target': 12000.0})
        sheet.action_pull_from_bas()
        # He only sells to T8001 (T8C collects): his one line is Salesman,
        # and it carries his sales target although his profile says both.
        self.assertEqual(line.role, 'sales')
        self.assertAlmostEqual(line.target_sales, 1000.0, 2)

    def test_sales_adjustment_survives_pull(self):
        env = self.env
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        line = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.march_rep)
        line.x_sales_adjustment = -250.0
        sheet.action_pull_from_bas()
        self.assertAlmostEqual(line.achieved_sales, 1000.0 - 250.0, 2)

    def test_one_line_per_type(self):
        """Same person sells and collects one customer (Salesman &
        Collector), only collects another (Collector), only sells a third
        (Salesman): three lines, each with only its own customers."""
        env = self.env
        Cust = env['ksw.bas.customer'].sudo()
        L = env['ksw.bas.ledger.line'].sudo()
        # T8002: T8C sells and collects. T8003: T8C sells, T8A collects.
        Cust.create({'bas_code': 'T8002', 'seller_code': 'T8C',
                     'collector_code': 'T8C', 'invoice_term_days': 30})
        Cust.create({'bas_code': 'T8003', 'seller_code': 'T8C',
                     'collector_code': 'T8A', 'invoice_term_days': 30})
        for key, acc, ftype, amt, seller, debit in (
                ('T8-S2', 'T8002', '002', 115.0, 'T8C', True),
                ('T8-C2', 'T8002', '018', 46.0, 'T8C', False),
                ('T8-S3', 'T8003', '002', 230.0, 'T8C', True)):
            L.create({'bas_key': key, 'ftype': ftype, 'date': '2030-03-25',
                      'debit_account': acc if debit else '',
                      'credit_account': '' if debit else acc,
                      'amount': amt, 'seller_code': seller})
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        mine = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.collector)
        by_type = {l.x_commission_type: l for l in mine}
        self.assertEqual(set(by_type), {'both', 'collect', 'sales'})
        self.assertAlmostEqual(by_type['both'].achieved_sales, 100.0, 2)
        self.assertAlmostEqual(by_type['both'].achieved_collection, 40.0, 2)
        self.assertAlmostEqual(by_type['sales'].achieved_sales, 200.0, 2)
        self.assertAlmostEqual(by_type['sales'].achieved_collection, 0.0, 2)
        # T8001: T8A sold, T8C collects
        self.assertAlmostEqual(by_type['collect'].achieved_sales, 0.0, 2)
        self.assertAlmostEqual(by_type['collect'].achieved_collection,
                               600.0, 2)
        self.assertAlmostEqual(by_type['collect'].target_collection,
                               500.0 / 1.15, 2)
        self.assertEqual(by_type['collect'].role, 'collect')

    def test_rule_per_type(self):
        env = self.env
        Rule = env['ksw.sales.commission.rule'].sudo()
        coll_rule = Rule.create({
            'name': 'T8 collector 1%', 'kind': 'collection',
            'scope': 'employee', 'employee_id': self.collector.id,
            'tier_ids': [(0, 0, {'from_pct': 0, 'to_pct': 0,
                                 'rate_pct': 1, 'base': 'achieved'})]})
        env['ksw.salesperson.profile'].sudo().create({
            'employee_id': self.collector.id, 'year': 2030, 'role': 'both',
            'x_type_rule_ids': [(0, 0, {'commission_type': 'collect',
                                        'rule_id': coll_rule.id})]})
        sheet = env['ksw.sales.commission.sheet'].sudo().create(
            {'period': '2030-03-01'})
        sheet.action_pull_from_bas()
        line = sheet.line_ids.filtered(
            lambda l: l.employee_id == self.collector
            and l.x_commission_type == 'collect')
        self.assertEqual(line.collection_rule_id, coll_rule)
        self.assertAlmostEqual(line.collection_commission_amount, 6.0, 2)
