from datetime import date

from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestRevisionRequestDisbursement(TransactionCase):
    """The cashier's reach into KSW_payroll's salary revision requests.

    The Loan Disbursement Officer confirms the handover of a revision paid
    in cash.  KSW_payroll cannot name this module's group, so the ACL, the
    record rule and the menu groups are declared here, and asserted here:
    KSW_payroll's own at_install tests run before this module is loaded.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cashier = cls.env['res.users'].create({
            'name': 'Revreq Disb Cashier', 'login': 'revreq_disb_cashier',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('KSW_deduction.group_loan_disbursement').id,
            ])],
        })
        employee = cls.env['hr.employee'].sudo().create({
            'name': 'Revreq Disb Employee'})
        Request = cls.env['ksw.payslip.revision.request'].sudo()

        def _request(method, state):
            slip = cls.env['hr.payslip'].sudo().create({
                'name': 'Revreq Disb %s %s' % (method, state),
                'employee_id': employee.id,
                'date_from': date(2026, 7, 1),
                'date_to': date(2026, 7, 31),
                'version_id': employee.current_version_id.id,
            })
            # sudo: the filing checks (a confirmed payslip, one open request
            # per payslip) are not what this class is about.
            return Request.create({
                'payslip_id': slip.id, 'reason': 'x',
                'payment_method': method, 'state': state,
            })

        cls.cash_pending = _request('cash', 'pending_disbursement')
        cls.cash_paid = _request('cash', 'paid')
        cls.bank_paid = _request('bank', 'paid')
        cls.cash_at_acc = _request('cash', 'pending_acc')

    def test_cashier_sees_cash_requests_from_disbursement_on(self):
        visible = self.env['ksw.payslip.revision.request'].with_user(
            self.cashier).search([('id', 'in', (
                self.cash_pending | self.cash_paid | self.bank_paid
                | self.cash_at_acc).ids)])
        self.assertEqual(visible, self.cash_pending | self.cash_paid)

    def test_cashier_finds_it_waiting_and_confirms(self):
        mine = self.env['ksw.payslip.revision.request'].with_user(
            self.cashier).search([('is_pending_my_action', '=', True)])
        self.assertIn(self.cash_pending, mine)
        self.assertNotIn(self.cash_paid, mine)
        req = self.cash_pending.with_user(self.cashier)
        req.write({'payment_reference': 'RCPT-1'})
        req.action_disbursement_confirm()
        self.assertEqual(self.cash_pending.state, 'paid')
        self.assertEqual(self.cash_pending.disbursed_by_id, self.cashier)

    def test_cashier_reaches_the_menus(self):
        """A child menu under a narrower parent is orphaned (pitfall #41):
        the Payroll root and the section must both be visible."""
        visible = self.env['ir.ui.menu'].with_user(
            self.cashier)._visible_menu_ids()
        for xmlid in ('om_hr_payroll.menu_hr_payroll_root',
                      'KSW_payroll.menu_ksw_revision_requests_root',
                      'KSW_payroll.menu_ksw_revision_requests_todo'):
            self.assertIn(self.env.ref(xmlid).id, visible, xmlid)
