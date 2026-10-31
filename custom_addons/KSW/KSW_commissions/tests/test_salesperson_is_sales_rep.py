"""The customer's Salesperson is the sales rep commissions credit (Oct 2026)."""
from odoo.tests import TransactionCase, new_test_user, tagged


@tagged('post_install', '-at_install')
class TestSalespersonIsSalesRep(TransactionCase):

    def test_the_salespersons_employee_overrides_the_bas_seller(self):
        rep_user = new_test_user(self.env, 'ksw_rep_user', groups='base.group_user')
        rep = self.env['hr.employee'].create({'name': 'Rep Employee', 'user_id': rep_user.id})
        partner = self.env['res.partner'].create({'name': 'Rep Client', 'user_id': rep_user.id})
        customer = self.env['ksw.bas.customer'].create({
            'bas_code': '1203ZZ777', 'name_ar': 'Rep Client', 'partner_id': partner.id,
            'seller_code': 'NOBODY', 'seller_name': 'Nobody'})
        customer._recompute_effective_reps()
        self.assertEqual(customer.effective_sales_rep_id, rep)

    def test_no_salesperson_falls_back_to_bas(self):
        partner = self.env['res.partner'].create({'name': 'No Rep Client'})
        self.assertFalse(partner._ksw_sales_rep_employee())

    def test_the_form_has_no_second_sales_rep(self):
        arch = self.env['res.partner'].get_views([(False, 'form')])['views']['form']['arch']
        self.assertNotIn('x_sales_rep_id', arch)
        self.assertIn('x_collection_rep_id', arch)
