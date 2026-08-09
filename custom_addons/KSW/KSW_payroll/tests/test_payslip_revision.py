# -*- coding: utf-8 -*-
"""Payslip revisions — re-issuing a period that was already paid.

Scenario this exists for: a payslip is confirmed and paid, the employee
complains the amount is short, the complaint turns out to be justified (a
time-off request approved after the batch closed, a lost weekend grant,
attendance that had not synced yet). Simply issuing a second payslip for
the month is wrong — HRA and GOSI are per-period amounts already settled
once, the loan installments have already been collected, and nothing
states how much more the employee is actually owed.

A revision recomputes the whole period with current data, reproduces the
installments the superseded payslip(s) collected, then subtracts everything
already paid via the PRIOR_NET rule. Its NET line *is* the difference.

Covered here:
  1. A period cannot be confirmed twice — both routes into ``done``.
  2. Exemption: a settled vacation payslip does not block the month.
  3. Issuing a revision: reopen-not-fork, state guards, no double PRIOR_HRA.
  4. Happy path — deserved / already paid / difference.
  5. Revision of a revision subtracts both predecessors.
  6. ACL — Officer may issue; a plain internal user may not.

Everything that involves `ksw.deduction` — frozen installments, pending
installments collected out of the difference, and the over-payment recovery
deduction — is covered by
`KSW_deduction/tests/test_payslip_revision_deductions.py`. It cannot live
here: KSW_deduction depends on this module, so it is not yet in the registry
when this suite runs during `-u KSW_payroll`.

The tests run on a purpose-built salary structure that deliberately omits
ATTDED. The test employee has no attendance records, so with ATTDED in play
every calendar day would be deducted as unpresented and the net would be
squeezed to near zero — which then trips `_ksw_apply_deduction_priority`
and makes installment amounts unpredictable. Attendance-driven deduction
is covered by its own suites.
"""
from datetime import date

from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPayslipRevision(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        Rule = cls.env['hr.salary.rule'].sudo()
        rule_codes = [
            'BASIC', 'HRA', 'GROSS', 'GOSI',
            'ADDITIONAL_COMMISSIONS', 'FLIGHT_TICKET', 'PENALTY',
            'KSW_DEDUCTIONS', 'PRIOR_NET', 'NET',
        ]
        rules = Rule.browse()
        for code in rule_codes:
            rule = Rule.search([('code', '=', code)], limit=1)
            if not rule:
                raise AssertionError('salary rule %s is missing' % code)
            rules |= rule
        cls.structure = cls.env['hr.payroll.structure'].sudo().create({
            'name': 'Revision Test Structure',
            'code': 'REVTEST',
            'rule_ids': [(6, 0, rules.ids)],
        })

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Revision Test Calendar',
            'tz': 'Asia/Riyadh',
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Revision Test Employee',
            'resource_calendar_id': cls.calendar.id,
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Revision Test Version',
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

        group_user = cls.env.ref('base.group_user')
        group_officer = cls.env.ref('om_hr_payroll.group_hr_payroll_user')
        # `hr.group_hr_manager` is not part of the revision gate — it is
        # what confirming ANY payslip already requires: the salary rules
        # read `contract.wage` inside safe_eval as the calling user, and
        # `wage` is manager-gated (pitfall #5). Without it this user could
        # not confirm an ordinary payslip either, so the fixture mirrors a
        # real payroll operator rather than isolating the new guard.
        group_hr_manager = cls.env.ref('hr.group_hr_manager')

        cls.user_officer = cls.env['res.users'].create({
            'name': 'Revision Officer',
            'login': 'revision_officer',
            'email': 'revision_officer@revision.test',
            'group_ids': [(6, 0, [group_user.id, group_officer.id,
                                  group_hr_manager.id])],
        })
        cls.user_plain = cls.env['res.users'].create({
            'name': 'Revision Plain User',
            'login': 'revision_plain',
            'email': 'revision_plain@revision.test',
            'group_ids': [(6, 0, [group_user.id])],
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_payslip(self, name='Original Slip', date_from=None,
                      date_to=None):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': name,
            'date_from': date_from or self.month_start,
            'date_to': date_to or self.month_end,
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

    def _add_input(self, slip, code, amount, name=None):
        """Add a one-time input, the way an officer edits a draft payslip."""
        return self.env['hr.payslip.input'].sudo().create({
            'payslip_id': slip.id,
            'version_id': self.version.id,
            'name': name or code,
            'code': code,
            'amount': amount,
        })

    def _issue_revision(self, slip):
        action = slip.with_user(self.user_officer).action_issue_revision()
        return self.env['hr.payslip'].browse(action['res_id'])

    # ==================================================================
    # 1. A period cannot be confirmed twice
    # ==================================================================

    def test_second_payslip_cannot_be_confirmed_via_button(self):
        first = self._confirm(self._make_payslip('First'))
        self.assertEqual(first.state, 'done')

        second = self._make_payslip('Second')
        with self.assertRaises(UserError):
            second.with_user(self.user_officer).action_payslip_done()
        self.assertNotEqual(second.state, 'done')

    def test_second_payslip_cannot_be_confirmed_via_raw_write(self):
        """The button is not the only way in — the plain RPC write must be
        guarded too (pitfall #37)."""
        self._confirm(self._make_payslip('First'))

        second = self._make_payslip('Second')
        with self.assertRaises(UserError):
            second.with_user(self.user_officer).write({'state': 'done'})
        self.assertNotEqual(second.state, 'done')

    def test_partial_overlap_also_blocks(self):
        """The guard is period *overlap*, not an exact match on the dates."""
        self._confirm(self._make_payslip('Full month'))

        overlapping = self._make_payslip(
            'Half month', date_from=date(2026, 7, 15),
            date_to=date(2026, 8, 14))
        with self.assertRaises(UserError):
            overlapping.with_user(self.user_officer).action_payslip_done()

    def test_other_period_is_unaffected(self):
        self._confirm(self._make_payslip('July'))

        august = self._make_payslip(
            'August', date_from=date(2026, 8, 1), date_to=date(2026, 8, 31))
        august.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(august.state, 'done')

    # ==================================================================
    # 2. Exemption — settled vacation payslip
    # ==================================================================

    def _make_vacation_leave(self, name, return_state):
        leave_type = self.env['hr.leave.type'].sudo().create({
            'name': 'Revision Test Leave (%s)' % name,
            'requires_allocation': False,
            'is_annual_leave': True,
        })
        leave = self.env['hr.leave'].sudo().create({
            'name': name,
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': date(2026, 7, 2),
            'request_date_to': date(2026, 7, 10),
        })
        leave.sudo().write({
            'state': 'validate',
            'x_return_state': return_state,
            'x_return_date': date(2026, 7, 11),
        })
        return leave

    def test_settled_vacation_payslip_does_not_block_the_month(self):
        """The employee went on vacation, was paid the vacation payslip,
        came back, and the direct manager confirmed the return. The rest of
        the month is genuinely owed as its own payslip."""
        leave = self._make_vacation_leave('Vacation settled', 'hr_confirmed')

        vacation_slip = self._make_payslip('Vacation Payslip')
        vacation_slip.sudo().write({'x_leave_id': leave.id})
        self._confirm(vacation_slip)
        self.assertTrue(vacation_slip._is_settled_vacation_payslip())

        monthly = self._make_payslip('Monthly after return')
        monthly.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(monthly.state, 'done')

    def test_vacation_payslip_with_open_return_still_blocks(self):
        """Same setup, but the manager has not confirmed the return yet, so
        the period is not settled and a second payslip is refused."""
        leave = self._make_vacation_leave('Vacation open', 'on_vacation')

        vacation_slip = self._make_payslip('Vacation Payslip open')
        vacation_slip.sudo().write({'x_leave_id': leave.id})
        vacation_slip.sudo().write({'state': 'done'})
        self.assertFalse(vacation_slip._is_settled_vacation_payslip())

        monthly = self._make_payslip('Monthly while away')
        with self.assertRaises(UserError):
            monthly.with_user(self.user_officer).action_payslip_done()

    # ==================================================================
    # 3 & 4. Issuing a revision
    # ==================================================================

    def test_revision_net_is_the_difference(self):
        """The core promise: deserved − already paid == what we still owe."""
        original = self._confirm(self._make_payslip('Original'))
        original_net = self._net(original)
        self.assertGreater(original_net, 0)

        revision = self._issue_revision(original)
        self.assertTrue(revision.x_is_revision)
        self.assertEqual(revision.x_revised_payslip_id, original)
        self.assertEqual(revision.x_prior_net_paid, original_net)
        self.assertEqual(self._input(revision, 'PRIOR_NET').amount,
                         original_net)

        # The complaint review finds an 800 SAR commission that belonged to
        # this period. The revision is a draft payslip — the officer adds it.
        self._add_input(revision, 'ADDITIONAL_COMMISSIONS', 800.0)

        # A revision is always allowed to confirm despite the done original.
        revision.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(revision.state, 'done')
        self.assertEqual(self._net(revision), 800.0)
        self.assertEqual(revision.x_deserved_net, original_net + 800.0)

    def test_revision_with_no_change_pays_nothing(self):
        """Re-issuing an unchanged period must come out at exactly zero —
        the sanity check that PRIOR_NET nets the period out completely."""
        original = self._confirm(self._make_payslip('Original unchanged'))
        revision = self._issue_revision(original)
        revision.compute_sheet()
        self.assertEqual(self._net(revision), 0.0)

    def test_prior_hra_is_not_subtracted_twice(self):
        """PRIOR_NET already nets out the HRA the original paid, so the
        revision must NOT also carry a PRIOR_HRA input."""
        original = self._confirm(self._make_payslip('Original HRA'))
        revision = self._issue_revision(original)

        self.assertFalse(self._input(revision, 'PRIOR_HRA'))
        self.assertFalse(self._input(revision, 'PRIOR_GOSI'))
        # Full HRA is present on the revision, exactly as on the original.
        hra = revision.line_ids.filtered(lambda l: l.code == 'HRA')
        self.assertEqual(hra.total, 1000.0)

    def test_revision_reopens_instead_of_forking(self):
        original = self._confirm(self._make_payslip('Original reopen'))
        first = self._issue_revision(original)
        second = self._issue_revision(original)
        self.assertEqual(first, second)

    def test_cannot_revise_a_draft_payslip(self):
        draft = self._make_payslip('Still draft')
        with self.assertRaises(UserError):
            draft.with_user(self.user_officer).action_issue_revision()

    def test_one_time_inputs_are_carried_over(self):
        """VACATION_BAL / FLIGHT_TICKET and friends must appear on the
        revision exactly once — it re-states the whole period."""
        original = self._make_payslip('Original with ticket')
        self._add_input(original, 'FLIGHT_TICKET', 1500.0)
        self._confirm(original)

        revision = self._issue_revision(original)
        carried = self._input(revision, 'FLIGHT_TICKET')
        self.assertEqual(len(carried), 1)
        self.assertEqual(carried.amount, 1500.0)

    # ==================================================================
    # 5. Chained revisions
    # ==================================================================

    def test_revision_of_a_revision_subtracts_both(self):
        original = self._confirm(self._make_payslip('Original chained'))
        original_net = self._net(original)

        first = self._issue_revision(original)
        self._add_input(first, 'ADDITIONAL_COMMISSIONS', 800.0)
        first.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(self._net(first), 800.0)

        # A second look finds another 200 owed for the same period.
        second = self._issue_revision(original)
        self.assertEqual(
            second.x_prior_net_paid, original_net + 800.0,
            'the second revision must subtract the original AND the first')

        # The first revision's commission is carried over as part of the
        # period; the new 200 is what is still outstanding.
        self._add_input(second, 'FLIGHT_TICKET', 200.0)
        second.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(self._net(second), 200.0)

    # ==================================================================
    # 6. ACL
    # ==================================================================

    def test_plain_user_cannot_issue_a_revision(self):
        original = self._confirm(self._make_payslip('Original acl'))
        with self.assertRaises(UserError):
            original.with_user(self.user_plain).action_issue_revision()

    def test_officer_can_issue_and_confirm_a_revision(self):
        original = self._confirm(self._make_payslip('Original officer'))
        revision = self._issue_revision(original)
        self._add_input(revision, 'ADDITIONAL_COMMISSIONS', 250.0)
        revision.with_user(self.user_officer).action_payslip_done()
        self.assertEqual(revision.state, 'done')
        self.assertEqual(self._net(revision), 250.0)
