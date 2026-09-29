# -*- coding: utf-8 -*-
"""Payroll Officers review payroll; only the system and Managers change it.

Audit 2026-09-28: an Officer could edit the NET line and inputs of a *paid*
payslip, recompute it, and raw-write it back to draft.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipValueLock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        g = cls.env.ref

        def _mkuser(login, groups):
            return cls.env['res.users'].create({
                'name': login, 'login': login, 'email': f'{login}@lock.test',
                'group_ids': [(6, 0, [x.id for x in groups])],
            })
        cls.officer = _mkuser('lock_officer', [
            g('base.group_user'), g('om_hr_payroll.group_hr_payroll_user')])
        cls.manager = _mkuser('lock_manager', [
            g('base.group_user'), g('om_hr_payroll.group_hr_payroll_manager')])
        cls.employee = cls.env['hr.employee'].create({'name': 'Lock Employee'})
        cls.rule = cls.env['hr.salary.rule'].search([], limit=1)

    def _slip(self, state='draft'):
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id, 'name': 'Lock slip',
            'date_from': date(2026, 6, 1), 'date_to': date(2026, 6, 30),
            'version_id': self.employee.version_id.id,
            'input_line_ids': [(0, 0, {'name': 'Bonus', 'code': 'LOCKBONUS',
                                       'amount': 100.0,
                                       'version_id': self.employee.version_id.id})],
            'line_ids': [(0, 0, {'name': 'Net', 'code': 'NET',
                                 'salary_rule_id': self.rule.id,
                                 'category_id': self.rule.category_id.id,
                                 'amount': 5000.0, 'quantity': 1, 'rate': 100,
                                 'version_id': self.employee.version_id.id})],
        })
        if state != 'draft':
            slip.write({'state': state})
        return slip

    # ── Officer: review only ────────────────────────────────────────
    def test_officer_cannot_edit_draft_values(self):
        slip = self._slip()
        with self.assertRaises(UserError):
            slip.input_line_ids.with_user(self.officer).write({'amount': 9999.0})
        with self.assertRaises(UserError):
            slip.line_ids.with_user(self.officer).write({'amount': 9999.0})
        with self.assertRaises(UserError):
            slip.with_user(self.officer).write({'date_to': date(2026, 6, 29)})
        with self.assertRaises(UserError):
            self.env['hr.payslip.input'].with_user(self.officer).create({
                'payslip_id': slip.id, 'name': 'x', 'code': 'X', 'amount': 1})
        with self.assertRaises(UserError):
            slip.input_line_ids.with_user(self.officer).unlink()

    def test_officer_cannot_touch_a_paid_payslip(self):
        slip = self._slip('done')
        net = slip.line_ids.amount
        with self.assertRaises(UserError):
            slip.line_ids.with_user(self.officer).write({'amount': net + 1000})
        with self.assertRaises(UserError):
            slip.with_user(self.officer).write({'state': 'draft'})
        with self.assertRaises(UserError):
            slip.with_user(self.officer).compute_sheet()
        self.assertEqual(slip.state, 'done')
        self.assertEqual(slip.line_ids.amount, net)

    def test_officer_cannot_confirm_or_delete(self):
        slip = self._slip()
        with self.assertRaises(UserError):
            slip.with_user(self.officer).action_payslip_done()
        with self.assertRaises(UserError):
            slip.with_user(self.officer).unlink()

    def test_officer_create_drops_hand_made_figures(self):
        slip = self.env['hr.payslip'].with_user(self.officer).create({
            'employee_id': self.employee.id, 'name': 'Officer slip',
            'date_from': date(2026, 7, 1), 'date_to': date(2026, 7, 31),
            'input_line_ids': [(0, 0, {'name': 'Fake', 'code': 'FAKE',
                                       'amount': 50000.0})],
        })
        self.assertFalse(slip.sudo().input_line_ids.filtered(
            lambda i: i.code == 'FAKE'))

    def test_officer_can_prepare_a_draft_batch(self):
        run = self.env['hr.payslip.run'].with_user(self.officer).create({
            'name': 'Officer batch', 'date_start': date(2026, 6, 1),
            'date_end': date(2026, 6, 30)})
        run.with_user(self.officer).write({'name': 'Officer batch (June)'})
        run.write({'state': 'close'})
        with self.assertRaises(UserError):
            run.with_user(self.officer).write({'name': 'renamed after close'})
        with self.assertRaises(UserError):
            run.with_user(self.officer).done_payslip_run()

    # ── Manager: corrects drafts, never a paid payslip ──────────────
    def test_manager_edits_draft_but_not_paid(self):
        slip = self._slip()
        slip.input_line_ids.with_user(self.manager).write({'amount': 150.0})
        self.assertEqual(slip.input_line_ids.amount, 150.0)
        paid = self._slip('done')
        with self.assertRaises(UserError):
            paid.line_ids.with_user(self.manager).write({'amount': 1.0})
        with self.assertRaises(UserError):
            paid.with_user(self.manager).write({'note': 'edited'}
                                               if 'note' in paid._fields
                                               else {'name': 'edited'})

    def test_manager_reversal_buttons_still_work(self):
        slip = self._slip('done')
        slip.with_user(self.manager).action_payslip_draft()
        self.assertEqual(slip.state, 'draft')
