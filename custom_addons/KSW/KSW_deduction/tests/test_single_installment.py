"""A deduction type locked to one installment stays one installment.

Since Oct 2026 every non-loan type starts locked (a non-loan deduction is
taken out of unpaid commission first, the rest by the next payslip). Only a
system administrator may lock or unlock a type, and the lock holds on every
route that sets the count: create, write, Reschedule Installments.
"""
from odoo.exceptions import UserError

from .common import DeductionCommon


class TestSingleInstallment(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.type_gov_pen.sudo().write({'x_single_installment': True})
        cls.admin_user = cls.env['res.users'].sudo().create({
            'name': 'Lock Admin', 'login': 'lock_admin',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('base.group_system').id,
                cls.env.ref('KSW_deduction.group_deduction_manager').id])],
        })
        cls.plain_user = cls.env['res.users'].sudo().create({
            'name': 'Lock Plain', 'login': 'lock_plain',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('KSW_deduction.group_deduction_manager').id])],
        })

    def test_01_create_is_forced_to_one(self):
        ded = self._make_deduction(self.type_gov_pen, amount=900.0,
                                   installments=3)
        self.assertEqual(ded.installments, 1)
        self._activate(ded)
        self.assertEqual(len(ded.line_ids), 1)
        self.assertAlmostEqual(ded.line_ids.amount, 900.0)

    def test_02_write_cannot_spread_it(self):
        ded = self._make_deduction(self.type_gov_pen, amount=900.0)
        with self.assertRaises(UserError):
            ded.write({'installments': 3})

    def test_03_only_the_system_administrator_changes_the_lock(self):
        with self.assertRaises(UserError):
            self.type_gov_pen.with_user(self.plain_user).write(
                {'x_single_installment': False})
        self.type_gov_pen.with_user(self.admin_user).write(
            {'x_single_installment': False})
        self.assertFalse(self.type_gov_pen.x_single_installment)
        ded = self._make_deduction(self.type_gov_pen, amount=900.0,
                                   installments=3)
        self.assertEqual(ded.installments, 3)

    def test_04_reschedule_cannot_spread_it(self):
        ded = self._activate(self._make_deduction(
            self.type_gov_pen, amount=900.0))
        wizard = self.env['ksw.deduction.reschedule.wizard'].create({
            'deduction_id': ded.id,
            'start_month': self.this_month,
            'reason': 'Spread it',
            'sibling_ids': [(6, 0, [])],
            'installments': 3,
        })
        with self.assertRaises(UserError):
            wizard.action_confirm()

    def test_05_a_loan_is_not_locked(self):
        self.assertFalse(self.type_loan.x_single_installment)
        ded = self._make_deduction(self.type_loan, amount=6000.0,
                                   installments=4)
        self.assertEqual(ded.installments, 4)
