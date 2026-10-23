from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestCostCentreColumns(TransactionCase):

    def test_distribution_split_into_the_two_bas_columns(self):
        Import = self.env['ksw.bas.gl.import']
        cc_plan, emp_plan = Import._cost_centre_root_plan(), Import._employee_root_plan()
        Account = self.env['account.analytic.account']
        truck = Account.create({'name': 'T197', 'plan_id': cc_plan.id})
        other = Account.create({'name': 'T198', 'plan_id': cc_plan.id})
        driver = Account.create({'name': 'DRIVER 1365', 'plan_id': emp_plan.id})
        expense = self.env['account.account'].create({
            'name': 'Test expense', 'code': '3999990001', 'account_type': 'expense'})
        bank = self.env['account.account'].create({
            'name': 'Test cash', 'code': '1999990001', 'account_type': 'asset_current'})
        move = self.env['account.move'].create({'move_type': 'entry', 'line_ids': [
            (0, 0, {'account_id': expense.id, 'debit': 100.0,
                    'analytic_distribution': {f'{truck.id},{driver.id}': 70.0, str(other.id): 30.0}}),
            (0, 0, {'account_id': bank.id, 'credit': 100.0}),
        ]})
        line, plain = move.line_ids.sorted('debit', reverse=True)
        self.assertEqual(line.x_cost_centre_id, truck, 'the largest share')
        self.assertEqual(line.x_employee_cost_centre_id, driver)
        self.assertFalse(plain.x_cost_centre_id)
        line.analytic_distribution = {str(other.id): 100.0}
        self.assertEqual(line.x_cost_centre_id, other)
        self.assertFalse(line.x_employee_cost_centre_id)
