# -*- coding: utf-8 -*-
"""Payslip revisions × deductions.

The revision feature itself lives in KSW_payroll (see
`KSW_payroll/tests/test_payslip_revision.py`). Everything that touches
`ksw.deduction` has to be tested from here instead: KSW_deduction depends
on KSW_payroll, so this model is not yet in the registry while the payroll
suite runs during `-u KSW_payroll`.

Three behaviours are at stake, and they are exactly the ones that made
"just issue another payslip" wrong in the first place:

* An installment the superseded payslip already collected is reproduced on
  the revision as a **frozen** `KSW_DEDP_*` input. It counts towards the
  deserved net — so the difference is not overstated by the amount already
  taken — but the ledger row keeps pointing at the ORIGINAL payslip and is
  never re-settled.
* An installment still **pending** for the period is injected as an
  ordinary `KSW_DED_*` and is collected out of the difference.
* A revision that comes out **below** what was already paid cannot be
  confirmed: it is cancelled and a draft recovery deduction is opened for
  the over-payment instead (a payslip cannot pay a negative amount, and the
  bank export drops negative-NET rows silently).
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipRevisionDeductions(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        # Purpose-built structure without ATTDED: the test employee has no
        # attendance records, so with ATTDED in play every calendar day
        # would be deducted as unpresented, squeezing the net towards zero
        # and making `_ksw_apply_deduction_priority` cap the installments to
        # unpredictable amounts. Attendance deduction has its own suites.
        Rule = cls.env['hr.salary.rule'].sudo()
        rules = Rule.browse()
        for code in ['BASIC', 'HRA', 'GROSS', 'GOSI', 'PENALTY',
                     'ADDITIONAL_COMMISSIONS', 'KSW_DEDUCTIONS',
                     'PRIOR_NET', 'NET']:
            rule = Rule.search([('code', '=', code)], limit=1)
            if not rule:
                raise AssertionError('salary rule %s is missing' % code)
            rules |= rule
        cls.structure = cls.env['hr.payroll.structure'].sudo().create({
            'name': 'Revision Deduction Test Structure',
            'code': 'REVDEDTEST',
            'rule_ids': [(6, 0, rules.ids)],
        })

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Revision Deduction Calendar',
            'tz': 'Asia/Riyadh',
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Revision Deduction Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Revision Deduction Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 5000.0,
            'hra': 1000.0,
            'da': 0.0,
            'travel_allowance': 0.0,
            'mobile_allowance': 0.0,
            'meal_allowance': 0.0,
            'medical_allowance': 0.0,
            'other_allowance': 0.0,
            'struct_id': cls.structure.id,
        })
        cls.employee._compute_current_version_id()

        cls.month_start = date(2026, 7, 1)
        cls.month_end = date(2026, 7, 31)

        # `hr.group_hr_manager` is not part of the revision gate — the
        # salary rules read `contract.wage` inside safe_eval as the calling
        # user and `wage` is manager-gated, so confirming ANY payslip
        # already needs it (pitfall #5).
        cls.user_officer = cls.env['res.users'].create({
            'name': 'Revision Deduction Officer',
            'login': 'revision_ded_officer',
            'email': 'revision_ded_officer@revision.test',
            'group_ids': [(6, 0, [
                cls.env.ref('base.group_user').id,
                cls.env.ref('om_hr_payroll.group_hr_payroll_user').id,
                cls.env.ref('hr.group_hr_manager').id,
            ])],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_payslip(self, name='Original Slip'):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': name,
            'date_from': self.month_start,
            'date_to': self.month_end,
            'struct_id': self.structure.id,
            'version_id': self.version.id,
        })

    def _confirm(self, slip):
        slip.action_payslip_done()
        return slip

    def _net(self, slip):
        return sum(
            slip.line_ids.filtered(lambda l: l.code == 'NET').mapped('total'))

    def _input(self, slip, code):
        return slip.input_line_ids.filtered(lambda i: i.code == code)

    def _add_input(self, slip, code, amount):
        return self.env['hr.payslip.input'].sudo().create({
            'payslip_id': slip.id,
            'version_id': self.version.id,
            'name': code,
            'code': code,
            'amount': amount,
        })

    def _issue_revision(self, slip):
        action = slip.with_user(self.user_officer).action_issue_revision()
        return self.env['hr.payslip'].browse(action['res_id'])

    def _make_deduction(self, amount, installments=1):
        """Active non-loan deduction with a generated installment schedule."""
        deduction = self.env['ksw.deduction'].sudo().create({
            'employee_id': self.employee.id,
            'type_id': self.env.ref('KSW_deduction.type_advance').id,
            'amount': amount,
            'installments': installments,
            'start_month': self.month_start,
        })
        deduction.sudo()._activate_and_generate_lines()
        return deduction

    # ==================================================================
    # Already-collected installments are frozen
    # ==================================================================

    def test_collected_installment_is_frozen_not_recollected(self):
        deduction = self._make_deduction(500.0)
        line = deduction.line_ids[0]

        original = self._confirm(self._make_payslip('Original with advance'))
        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.payslip_id, original)

        revision = self._issue_revision(original)

        frozen = self._input(revision, 'KSW_DEDP_%d' % line.id)
        self.assertTrue(frozen, 'collected installment must be reproduced')
        self.assertEqual(frozen.amount, 500.0)
        # …and it must NOT be re-injected as a collectable input.
        self.assertFalse(self._input(revision, 'KSW_DED_%d' % line.id))

        revision.with_user(self.user_officer).action_payslip_done()

        line.invalidate_recordset()
        self.assertEqual(line.state, 'paid')
        self.assertEqual(
            line.payslip_id, original,
            'a revision must not re-settle an already-collected installment')

    def test_frozen_installment_counts_towards_deserved_net(self):
        """KSW_DEDUCTIONS sums the shorter `KSW_DED` prefix, so the frozen
        rows reach the payslip total — an unchanged period therefore still
        nets to exactly zero rather than paying the installment back."""
        self._make_deduction(500.0)
        original = self._confirm(self._make_payslip('Original deserved'))
        revision = self._issue_revision(original)

        ksw_line = revision.line_ids.filtered(
            lambda l: l.code == 'KSW_DEDUCTIONS')
        self.assertTrue(ksw_line)
        self.assertEqual(ksw_line.total, -500.0)
        self.assertEqual(self._net(revision), 0.0)

    # ==================================================================
    # Pending installments are collected from the difference
    # ==================================================================

    def test_pending_installment_is_collected_from_the_difference(self):
        original = self._confirm(self._make_payslip('Original pending'))
        original_net = self._net(original)

        revision = self._issue_revision(original)
        # Money owed for the period, found during the complaint review …
        self._add_input(revision, 'ADDITIONAL_COMMISSIONS', 800.0)
        # … and a penalty raised in the meantime.
        deduction = self._make_deduction(300.0)
        line = deduction.line_ids[0]
        self.assertEqual(line.state, 'pending')

        revision.with_user(self.user_officer).action_payslip_done()

        self.assertEqual(self._net(revision), 500.0,
                         '800 owed less the 300 penalty now due')
        line.invalidate_recordset()
        self.assertEqual(line.state, 'paid')
        self.assertEqual(line.payslip_id, revision)
        self.assertEqual(revision.x_prior_net_paid, original_net)

    # ==================================================================
    # Over-payment
    # ==================================================================

    def test_overpayment_cancels_revision_and_opens_a_deduction(self):
        original = self._confirm(self._make_payslip('Original overpaid'))
        original_net = self._net(original)

        revision = self._issue_revision(original)
        # A penalty that should have been charged in the original period.
        self._add_input(revision, 'PENALTY', 400.0)
        revision.compute_sheet()
        self.assertEqual(self._net(revision), -400.0)

        Deduction = self.env['ksw.deduction']
        before = Deduction.sudo().search([
            ('employee_id', '=', self.employee.id)])

        result = revision.with_user(self.user_officer).action_payslip_done()

        self.assertEqual(revision.state, 'cancel')
        self.assertEqual(result.get('tag'), 'display_notification')

        created = Deduction.sudo().search([
            ('employee_id', '=', self.employee.id),
            ('id', 'not in', before.ids),
        ])
        self.assertEqual(len(created), 1)
        self.assertEqual(created.amount, 400.0)
        self.assertEqual(created.installments, 1)
        self.assertEqual(created.state, 'draft')
        self.assertEqual(revision.x_prior_net_paid, original_net)

    def test_overpayment_recovery_type_is_configurable(self):
        self.env['ir.config_parameter'].sudo().set_param(
            'ksw_payroll.overpay_recovery_type_id',
            str(self.env.ref('KSW_deduction.type_internal_penalty').id))

        original = self._confirm(self._make_payslip('Original overpaid cfg'))
        revision = self._issue_revision(original)
        self._add_input(revision, 'PENALTY', 250.0)
        revision.compute_sheet()

        before = self.env['ksw.deduction'].sudo().search([
            ('employee_id', '=', self.employee.id)])
        revision.with_user(self.user_officer).action_payslip_done()
        created = self.env['ksw.deduction'].sudo().search([
            ('employee_id', '=', self.employee.id),
            ('id', 'not in', before.ids),
        ])
        self.assertEqual(
            created.type_id,
            self.env.ref('KSW_deduction.type_internal_penalty'))

    def test_overpayment_via_raw_write_is_refused(self):
        """The write() route cannot return a notification, so it refuses —
        and because a UserError rolls the transaction back, it must not try
        to create the recovery deduction there either."""
        original = self._confirm(self._make_payslip('Original overpaid rpc'))
        revision = self._issue_revision(original)
        self._add_input(revision, 'PENALTY', 400.0)
        revision.compute_sheet()

        with self.assertRaises(UserError):
            revision.with_user(self.user_officer).write({'state': 'done'})
        self.assertNotEqual(revision.state, 'done')
