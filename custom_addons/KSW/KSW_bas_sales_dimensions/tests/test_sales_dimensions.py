from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, new_test_user, tagged


class _Cursor:
    """A BAS cursor answering the three queries the tagging makes."""

    def __init__(self, wref, sellers, notes):
        self.wref, self.sellers, self.notes, self.last = wref, sellers, notes, []

    def execute(self, sql, params=()):
        if 'WREF10' in sql:
            self.last = [{'code': c, 'NAME': n} for c, n in self.wref]
        elif 'SELLER' in sql:
            self.last = [{'N': n, 'seller': s} for n, s in self.sellers if n in params]
        else:
            self.last = [{'N': n, 'cc1': a, 'cc2': b} for n, a, b in self.notes if n in params]

    def fetchall(self):
        return self.last


@tagged('post_install', '-at_install')
class TestSalesDimensions(TransactionCase):

    def test_invoice_gets_salesperson_and_each_line_its_vehicle_and_driver(self):
        Import = self.env['ksw.bas.gl.import']
        cc_plan, emp_plan = Import._cost_centre_root_plan(), Import._employee_root_plan()
        Account = self.env['account.analytic.account']
        branch = Account.create({'name': 'Branch X', 'code': 'ZB1', 'x_bas_code': 'ZB1', 'plan_id': cc_plan.id})
        truck = Account.create({'name': 'T-ZZ1', 'code': 'ZV1', 'x_bas_code': 'ZV1', 'plan_id': cc_plan.id})
        user = new_test_user(self.env, 'zz_rep', groups='base.group_user')
        emp = self.env['hr.employee'].create({'name': 'Rep ZZ', 'user_id': user.id})
        self.env['ksw.bas.sales.rep'].create({'code': 'Z77', 'employee_id': emp.id})
        partner = self.env['res.partner'].create({'name': 'Client ZZ'})
        product = self.env['product.product'].create({'name': 'Water ZZ'})
        picking = self.env['stock.picking'].create({
            'picking_type_id': self.env.ref('stock.picking_type_out').id, 'x_bas_key': '600/3/999/11'})
        income = self.env['account.account'].create({
            'name': 'Sales ZZ', 'code': '4999990001', 'account_type': 'income'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-07-31',
            'x_bas_key': '002/3/999/22',
            'invoice_line_ids': [
                (0, 0, {'name': f'{picking.name} · 2026-07-01', 'product_id': product.id,
                        'quantity': 1, 'price_unit': 544, 'account_id': income.id,
                        'tax_ids': [(6, 0, [])], 'analytic_distribution': {str(branch.id): 100}}),
                (0, 0, {'name': 'Other line', 'quantity': 1, 'price_unit': 10, 'account_id': income.id,
                        'tax_ids': [(6, 0, [])], 'analytic_distribution': {str(branch.id): 100}}),
            ]})
        inv.action_post()
        self.assertEqual(inv.invoice_user_id, self.env.user, 'created by the importing user')
        cursor = _Cursor(wref=[('ZV1', 'T-ZZ1'), ('ZD1', 'DRIVER ZZ 12')],
                         sellers=[(22, 'Z77')], notes=[(11, 'T-ZZ1', 'DRIVER\xa0ZZ  12')])
        conn = MagicMock()
        conn.cursor.return_value = cursor
        with patch.object(type(Import), '_bas_connect', return_value=conn):
            res = Import.action_tag_sales_dimensions('2026-07-01', '2026-07-31', commit=False)
        self.assertEqual(inv.invoice_user_id, user)
        note_line, other = inv.invoice_line_ids.sorted('price_unit', reverse=True)
        self.assertEqual(note_line.x_cost_centre_id, truck, 'the vehicle, not the branch')
        self.assertEqual(note_line.x_employee_cost_centre_id.x_bas_code, 'ZD1')
        self.assertEqual(note_line.x_salesperson_id, user)
        self.assertEqual(other.x_cost_centre_id, branch, 'a line that is not a delivery note is left alone')
        self.assertEqual(inv.amount_total, 554)
        self.assertEqual(res['lines tagged'], 1)

    def test_unmapped_or_house_rep_clears_odoobot(self):
        Import = self.env['ksw.bas.gl.import']
        partner = self.env['res.partner'].create({'name': 'Client ZY'})
        income = self.env['account.account'].create({
            'name': 'Sales ZY', 'code': '4999990002', 'account_type': 'income'})
        inv = self.env['account.move'].create({
            'move_type': 'out_invoice', 'partner_id': partner.id, 'invoice_date': '2026-07-31',
            'x_bas_key': '002/3/998/23', 'invoice_user_id': self.env.ref('base.user_root').id,
            'invoice_line_ids': [(0, 0, {'name': 'x', 'quantity': 1, 'price_unit': 10,
                                         'account_id': income.id, 'tax_ids': [(6, 0, [])]})]})
        inv.action_post()
        conn = MagicMock()
        conn.cursor.return_value = _Cursor(wref=[], sellers=[(23, '1033')], notes=[])
        with patch.object(type(Import), '_bas_connect', return_value=conn):
            res = Import.action_tag_sales_dimensions('2026-07-01', '2026-07-31', commit=False)
        self.assertFalse(inv.invoice_user_id)
        self.assertEqual(res['unmapped reps'], {'1033': 1})
