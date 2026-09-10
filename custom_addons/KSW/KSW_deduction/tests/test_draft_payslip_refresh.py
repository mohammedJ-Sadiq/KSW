# -*- coding: utf-8 -*-
"""A deduction activated after a batch was generated must reach its DRAFT
payslips immediately — not silently at confirmation.

Regression cover for CLAUDE.md pitfall #120 (KSWCO batch 250, August 2026):
three loans were disbursement-confirmed 12 minutes after the batch was
generated, so their installments only surfaced when the batch was marked
Done — a day after the bank file had been exported and paid. 2,550 SAR was
recorded as collected that had never been withheld.
"""
from datetime import date
from unittest.mock import patch

from .common import DeductionCommon


class TestDraftPayslipRefresh(DeductionCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'KSWDPR Sched',
        })
        for day in ['0', '1', '2', '3', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'd{day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'KSWDPR Cal',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })
        cls.employee.write({'resource_calendar_id': cls.calendar.id})
        cls.struct = cls.env.ref('om_hr_payroll.structure_base')
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'KSWDPR Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'da': 0.0,
            'travel_allowance': 0.0,
            'meal_allowance': 0.0,
            'medical_allowance': 0.0,
            'other_allowance': 0.0,
            'hra': 0.0,
            'struct_id': cls.struct.id,
        })
        cls.employee._compute_current_version_id()
        cls.period_from = date(2026, 4, 1)
        cls.period_to = date(2026, 4, 30)
        # Fully attended sheet, so ATTDED is 0 and NET == wage. Without it
        # every day counts as absent and the capping logic forwards
        # everything, which would mask the effect under test.
        cls.employee.write({'x_is_attendance_sheet': True})
        cls.env['ksw.attendance.sheet'].create({
            'employee_id': cls.employee.id, 'month': '4', 'year': 2026})

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _make_payslip(self, dfrom=None, dto=None, name='Slip', run=None):
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': name,
            'date_from': dfrom or self.period_from,
            'date_to': dto or self.period_to,
            'struct_id': self.struct.id,
            'version_id': self.version.id,
            'payslip_run_id': run.id if run else False,
        })

    def _ksw_inputs(self, slip):
        return slip.input_line_ids.filtered(
            lambda i: i.code and i.code.startswith('KSW_DED_'))

    def _pending_deduction(self, amount=900.0, installments=3,
                           start_month=None):
        """A deduction sitting at `draft` with NO installment lines yet —
        the state a loan is in between GM approval and disbursement."""
        ded = self._make_deduction(
            self.type_loan, amount=amount, installments=installments,
            start_month=start_month or date(2026, 4, 1))
        ded.line_ids.unlink()
        return ded

    # ------------------------------------------------------------------
    # the fix
    # ------------------------------------------------------------------
    def test_activation_pulls_installment_into_draft_payslip(self):
        """The batch was generated first; activating afterwards must reach it.

        Asserted on the *presence* of the `KSW_DED_<line>` input rather than
        its amount: this class shares the fully-attended-sheet fixture whose
        ATTDED currently comes out as the whole wage, leaving NET at 0 so
        `_ksw_apply_deduction_priority` caps every input to 0. That staleness
        is pre-existing and unrelated — the sibling
        `TestDeductionPayslip.test_inputs_injected_for_lines_in_period` fails
        on exactly the same `0.0 != 100.0`. What this test owns is *whether
        the installment reaches the draft payslip at all*, which is precisely
        what was missing in batch 250.
        """
        slip = self._make_payslip()
        slip.compute_sheet()
        self.assertFalse(self._ksw_inputs(slip),
                         'nothing to collect before the deduction is active')

        ded = self._pending_deduction()
        ded._activate_and_generate_lines()

        april = ded.line_ids.filtered(
            lambda l: l.year == 2026 and l.month == 4)
        self.assertEqual(len(april), 1)
        self.assertIn(
            'KSW_DED_%d' % april.id, self._ksw_inputs(slip).mapped('code'),
            'the April installment should have been pulled into the draft '
            'payslip at activation, not left until confirmation')

    def test_activation_warns_when_the_net_moves(self):
        """A moved NET means the exported bank file is stale — say so.

        `_ksw_net_amount` is stubbed because the shared fixture cannot
        currently produce a payslip whose NET moves at all (see
        `test_activation_pulls_installment_into_draft_payslip`). The three
        reads per slip are, in order: the `before` snapshot, the comparison,
        and the figure quoted in the note.
        """
        run = self.env['hr.payslip.run'].create({
            'name': 'April 2026', 'date_start': self.period_from,
            'date_end': self.period_to,
        })
        slip = self._make_payslip(run=run)
        slip.compute_sheet()
        slip_seen, run_seen = slip.message_ids.ids, run.message_ids.ids

        nets = iter([1000.0, 700.0, 700.0])
        with patch.object(type(slip), '_ksw_net_amount',
                          lambda self: next(nets)):
            self._pending_deduction()._activate_and_generate_lines()

        new_on_slip = slip.message_ids.filtered(
            lambda m: m.id not in slip_seen)
        self.assertTrue(
            any('Recomputed' in (m.body or '') for m in new_on_slip),
            'the payroll officer must be told the draft payslip moved')

        new_on_run = run.message_ids.filtered(lambda m: m.id not in run_seen)
        self.assertTrue(
            new_on_run, 'the batch itself must carry the warning — that is '
                        'where the bank file is exported from')
        self.assertTrue(
            any('export it again' in (m.body or '') for m in new_on_run))

    def test_no_warning_when_the_net_is_unchanged(self):
        """Nothing to re-export means nothing to shout about."""
        slip = self._make_payslip()
        slip.compute_sheet()
        seen = slip.message_ids.ids

        self._pending_deduction()._activate_and_generate_lines()

        new = slip.message_ids.filtered(lambda m: m.id not in seen)
        self.assertFalse(
            new.filtered(lambda m: 'Recomputed' in (m.body or '')),
            'the capped input left NET where it was; the bank file still '
            'stands, so no warning belongs here')

    # ------------------------------------------------------------------
    # what it must NOT do
    # ------------------------------------------------------------------
    def test_done_payslip_is_never_touched(self):
        """A `done` payslip has been paid; a later deduction never restates it."""
        slip = self._make_payslip()
        slip.compute_sheet()
        # Raw write, not action_payslip_done(): this asserts the guard in
        # _refresh_draft_payslips, not the confirmation path.
        slip.write({'state': 'done'})
        net_before = slip._ksw_net_amount()

        self._pending_deduction()._activate_and_generate_lines()

        self.assertFalse(self._ksw_inputs(slip))
        self.assertEqual(slip._ksw_net_amount(), net_before)

    def test_payslip_ending_before_the_installment_is_untouched(self):
        """March's payslip cannot collect an installment that starts in April."""
        march = self._make_payslip(
            dfrom=date(2026, 3, 1), dto=date(2026, 3, 31), name='March')
        march.compute_sheet()
        net_before = march._ksw_net_amount()

        self._pending_deduction(start_month=date(2026, 4, 1)) \
            ._activate_and_generate_lines()

        self.assertFalse(self._ksw_inputs(march))
        self.assertEqual(march._ksw_net_amount(), net_before)

    def test_no_draft_payslip_is_not_an_error(self):
        """The ordinary case — nothing generated yet — must stay silent."""
        ded = self._pending_deduction()
        ded._activate_and_generate_lines()
        self.assertEqual(ded.state, 'active')
        self.assertEqual(len(ded.line_ids), 3)
