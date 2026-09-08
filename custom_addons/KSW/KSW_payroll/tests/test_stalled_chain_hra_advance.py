# -*- coding: utf-8 -*-
"""The HRA / GOSI advance may only cover months the employee actually missed.

The vacation payslip pays housing allowance up front for every paid
vacation month because an employee who is away draws no ordinary
payslip for those months — the monthly batch skips anyone whose return
is still pending.

A request that stalls in the approval chain breaks that assumption.  The
employee never leaves, the batch pays him every month as usual, and by
the time the chain finally completes those months are settled in full.
Advancing them again hands him the housing allowance twice and takes
GOSI off him twice.

Scenario (KSWCO leave 4882): requested 29 Jul → 8 Aug, left pending, so
the July and August payslips were issued normally.  The chain resumes in
September: the advance must be zero.

Employee: wage=6000  hra=1500 → GOSI = round(7500 × 9.75%) = 731/month.
"""
from datetime import date, datetime, time as dt_time

from odoo.tests.common import TransactionCase


class TestStalledChainHraAdvance(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.calendar_group = cls.env['resource.calendar.group'].create({
            'name': 'Stalled Chain Group',
        })
        for day in ['0', '1', '2', '3', '4', '5', '6']:
            cls.env['resource.calendar.group.line'].create({
                'name': f'Work {day}',
                'calendar_group_id': cls.calendar_group.id,
                'dayofweek': day,
                'day_period': 'full_day',
                'hour_from': 8.0,
                'hour_to': 16.5,
            })
        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Stalled Chain Calendar',
            'tz': 'Asia/Riyadh',
            'calendar_group_ids': [(4, cls.calendar_group.id)],
        })

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Stalled Chain Employee',
            'resource_calendar_id': cls.calendar.id,
            'country_id': cls.env.ref('base.sa').id,   # Saudi → GOSI
        })
        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Stalled Chain Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 6000.0,
            'da': 0.0,
            'travel_allowance': 500.0,
            'meal_allowance': 300.0,
            'medical_allowance': 200.0,
            'other_allowance': 0.0,
            'hra': 1500.0,
            'struct_id': cls.env.ref('om_hr_payroll.structure_base').id,
        })
        cls.employee._compute_current_version_id()

        cls.leave_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Stalled Chain Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

        cls.HRA = 1500.0
        cls.GOSI = round((6000.0 + 1500.0) * 9.75 / 100.0)   # 731

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_leave(self, date_from, date_to):
        """Insert a pending annual leave directly (ORM constraints aside)."""
        cal_days = (date_to - date_from).days + 1
        self.env.cr.execute("""
            INSERT INTO hr_leave
                (employee_id, holiday_status_id, state,
                 request_date_from, request_date_to, date_from, date_to,
                 number_of_days, number_of_hours,
                 x_return_state, x_annual_approval_state,
                 create_uid, write_uid, create_date, write_date)
            VALUES (%s, %s, 'confirm', %s, %s, %s, %s, %s, %s,
                    'not_applicable', 'pending_gm_final',
                    %s, %s, NOW(), NOW())
            RETURNING id
        """, (
            self.employee.id, self.leave_type.id,
            date_from, date_to,
            datetime.combine(date_from, dt_time(5, 0)),
            datetime.combine(date_to, dt_time(13, 30)),
            cal_days, cal_days * 8.5,
            self.env.uid, self.env.uid,
        ))
        leave_id = self.env.cr.fetchone()[0]
        self.env.invalidate_all()
        return self.env['hr.leave'].browse(leave_id)

    def _issue_monthly_payslip(self, month_start, month_end, state='done'):
        """An ordinary monthly payslip, computed and marked issued.

        Raw ``write`` on the state rather than ``action_payslip_done()``:
        the point here is only that the month carries an HRA and a GOSI
        line, not to re-run the confirmation machinery.
        """
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Monthly %s' % month_start,
            'date_from': month_start,
            'date_to': month_end,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })
        slip.compute_sheet()
        if state != 'draft':
            slip.write({'state': state})
        return slip

    def _vacation_payslip(self, leave, month_start, month_end):
        """The (September) payslip the resumed chain would produce."""
        return self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Vacation Payslip',
            'date_from': month_start,
            'date_to': month_end,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
            'x_leave_id': leave.id,
        })

    def _advance(self, leave, code):
        """The advance input value the builder produces for ``code``."""
        payslip = self._vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))
        vals = leave._build_vacation_input_lines(
            leave, self.employee, payslip)
        return next((v for v in vals if v['code'] == code), None)

    def _issue_vacation_payslip(self, leave, month_start, month_end,
                                state='done'):
        """The definitive vacation payslip a completed chain produces."""
        slip = self._vacation_payslip(leave, month_start, month_end)
        vals = leave._build_vacation_input_lines(leave, self.employee, slip)
        if vals:
            self.env['hr.payslip.input'].create(vals)
        slip.compute_sheet()
        if state != 'draft':
            slip.write({'state': state})
        return slip

    def _monthly(self, month_start, month_end):
        """An ordinary monthly payslip, computed but left draft."""
        slip = self.env['hr.payslip'].create({
            'employee_id': self.employee.id,
            'name': 'Monthly %s' % month_start,
            'date_from': month_start,
            'date_to': month_end,
            'struct_id': self.env.ref('om_hr_payroll.structure_base').id,
            'version_id': self.version.id,
        })
        slip.compute_sheet()
        return slip

    def _line(self, slip, code):
        line = slip.line_ids.filtered(lambda l: l.code == code)[:1]
        return line.total if line else 0.0

    def _input(self, slip, code):
        inp = slip.input_line_ids.filtered(lambda i: i.code == code)[:1]
        return inp.amount if inp else 0.0

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test_advance_covers_every_month_the_employee_missed(self):
        """Nothing issued for July/August → both months are advanced."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))

        hra = self._advance(leave, 'VACATION_HRA')
        self.assertTrue(hra, 'A missed month must still be advanced.')
        self.assertEqual(hra['amount'], self.HRA * 2)
        self.assertIn('Jul 2026', hra['name'])
        self.assertIn('Aug 2026', hra['name'])

        gosi = self._advance(leave, 'VACATION_GOSI')
        self.assertTrue(gosi)
        self.assertEqual(gosi['amount'], self.GOSI * 2)

    def test_advance_skips_months_already_paid(self):
        """July and August already issued → nothing left to advance."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        july = self._issue_monthly_payslip(date(2026, 7, 1), date(2026, 7, 31))
        august = self._issue_monthly_payslip(date(2026, 8, 1), date(2026, 8, 31))

        # The fixture is only meaningful if those payslips really paid HRA.
        for slip in (july, august):
            self.assertTrue(
                slip.line_ids.filtered(lambda l: l.code == 'HRA' and l.total),
                'Fixture: the monthly payslip should carry an HRA line.')

        self.assertIsNone(
            self._advance(leave, 'VACATION_HRA'),
            'HRA was already paid in the July and August payslips.')
        self.assertIsNone(
            self._advance(leave, 'VACATION_GOSI'),
            'GOSI was already deducted in the July and August payslips.')

    def test_advance_covers_only_the_month_still_missing(self):
        """July issued, August not → one month of advance, August's."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        self._issue_monthly_payslip(date(2026, 7, 1), date(2026, 7, 31))

        hra = self._advance(leave, 'VACATION_HRA')
        self.assertTrue(hra)
        self.assertEqual(hra['amount'], self.HRA)
        self.assertIn('Aug 2026', hra['name'])
        self.assertNotIn('Jul 2026', hra['name'])

        gosi = self._advance(leave, 'VACATION_GOSI')
        self.assertTrue(gosi)
        self.assertEqual(gosi['amount'], self.GOSI)

    def test_a_draft_payslip_settles_nothing(self):
        """A draft July payslip is not money out of the door."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        self._issue_monthly_payslip(
            date(2026, 7, 1), date(2026, 7, 31), state='draft')

        hra = self._advance(leave, 'VACATION_HRA')
        self.assertTrue(hra)
        self.assertEqual(hra['amount'], self.HRA * 2)

    def test_the_leaves_own_payslip_is_not_a_prior_payment(self):
        """Recomputing the vacation payslip must not cancel its own advance."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        own = self._vacation_payslip(leave, date(2026, 9, 1), date(2026, 9, 30))
        self.env['hr.payslip.input'].create({
            'payslip_id': own.id,
            'version_id': self.version.id,
            'name': 'Advance HRA',
            'code': 'VACATION_HRA',
            'amount': self.HRA * 2,
        })
        own.compute_sheet()
        own.write({'state': 'done'})

        hra = self._advance(leave, 'VACATION_HRA')
        self.assertTrue(hra, 'A leave never counts its own payslip as prior.')
        self.assertEqual(hra['amount'], self.HRA * 2)

    def test_a_skipped_month_is_explained_on_the_chatter(self):
        """The approver must be able to tell an exclusion from a bug."""
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        july = self._issue_monthly_payslip(date(2026, 7, 1), date(2026, 7, 31))

        payslip = self._vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))
        leave._build_vacation_input_lines(leave, self.employee, payslip)

        bodies = ' '.join(payslip.message_ids.mapped('body'))
        self.assertIn('Jul 2026', bodies)
        self.assertIn(july.number or july.name, bodies)

    def test_the_explanation_is_posted_once_not_once_per_recompute(self):
        """The builder re-runs on every recompute of a vacation payslip.

        ``hr.payslip._refresh_vacation_bal_input`` calls it just to
        re-derive VACATION_BAL, so a plain message_post stacks an
        identical note on the chatter each time the payslip is computed.
        """
        leave = self._create_leave(date(2026, 7, 29), date(2026, 8, 8))
        self._issue_monthly_payslip(date(2026, 7, 1), date(2026, 7, 31))
        payslip = self._vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))

        for _i in range(3):
            leave._build_vacation_input_lines(leave, self.employee, payslip)

        notes = payslip.message_ids.filtered(
            lambda m: 'Advance skipped' in (m.body or ''))
        self.assertEqual(len(notes), 1, 'One note, however often it recomputes.')

    # ------------------------------------------------------------------
    # The mirror direction: the monthly payslip must not re-pay a month
    # the vacation payslip already advanced.  The advance is the vacation
    # payslip's ONLY housing line — the ordinary HRA rule is suppressed on
    # it — so a guard that looks for an 'HRA' line sees nothing.
    # ------------------------------------------------------------------

    def test_monthly_payslip_does_not_repay_an_advanced_month(self):
        """October was advanced in September's vacation payslip."""
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        vac = self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(self._line(vac, 'VACATION_HRA'), self.HRA * 2,
                         'Fixture: Sep + Oct advanced together.')

        october = self._monthly(date(2026, 10, 1), date(2026, 10, 31))

        self.assertEqual(self._input(october, 'PRIOR_HRA'), self.HRA,
                         'October must see its own share of the advance.')
        self.assertEqual(self._line(october, 'HRA'), 0.0,
                         'The housing allowance was already advanced.')
        self.assertEqual(self._input(october, 'PRIOR_GOSI'), self.GOSI)
        self.assertEqual(self._line(october, 'GOSI'), 0.0)

    def test_a_month_outside_the_advance_keeps_its_own_hra(self):
        """November was never advanced — it is paid in full."""
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))

        november = self._monthly(date(2026, 11, 1), date(2026, 11, 30))

        self.assertEqual(self._input(november, 'PRIOR_HRA'), 0.0)
        self.assertEqual(self._line(november, 'HRA'), self.HRA)
        self.assertEqual(self._line(november, 'GOSI'), -self.GOSI)

    def test_a_draft_vacation_payslip_advances_nothing(self):
        """Nothing has been paid until the vacation payslip is issued."""
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30), state='draft')

        october = self._monthly(date(2026, 10, 1), date(2026, 10, 31))

        self.assertEqual(self._input(october, 'PRIOR_HRA'), 0.0)
        self.assertEqual(self._line(october, 'HRA'), self.HRA)

    def test_the_share_follows_the_months_actually_advanced(self):
        """September already paid → the advance is October's alone, whole.

        The share is the lump divided by the months it *covers*, not by
        the months the leave spans — those differ exactly when the first
        fix drops an already-settled month.
        """
        self._issue_monthly_payslip(date(2026, 9, 1), date(2026, 9, 30))
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        vac = self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))
        self.assertEqual(self._line(vac, 'VACATION_HRA'), self.HRA,
                         'Fixture: only October is advanced.')

        october = self._monthly(date(2026, 10, 1), date(2026, 10, 31))

        self.assertEqual(
            self._input(october, 'PRIOR_HRA'), self.HRA,
            'October carries the whole advance, not half of it.')
        self.assertEqual(self._line(october, 'HRA'), 0.0)

    def test_the_covered_months_are_recorded_on_the_advance(self):
        """The input line says which months it pays for."""
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        vac = self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30))

        inp = vac.input_line_ids.filtered(lambda i: i.code == 'VACATION_HRA')
        self.assertEqual(inp.x_ksw_advance_months, '2026-09,2026-10')
        self.assertEqual(inp._ksw_advance_month_list(),
                         [(2026, 9), (2026, 10)])

    # ------------------------------------------------------------------
    # A revision restates ONE payslip: what it paid against what it should
    # have paid.  An allowance another document already covered makes this
    # payslip's own line undeserved — that difference is the whole point.
    # ------------------------------------------------------------------

    def _confirmed_monthly_before_the_advance(self, leave, month_start,
                                              month_end):
        """The historical mistake: a monthly issued while the vacation
        payslip was still draft, so it paid the allowance itself."""
        vac = self._issue_vacation_payslip(
            leave, date(2026, 9, 1), date(2026, 9, 30), state='draft')
        monthly = self._monthly(month_start, month_end)
        self.assertEqual(self._line(monthly, 'HRA'), self.HRA,
                         'Fixture: the monthly paid the allowance itself.')
        monthly.write({'state': 'done'})
        vac.write({'state': 'done'})     # confirmed afterwards
        return monthly, vac

    def test_revision_claws_back_an_allowance_paid_by_another_document(self):
        """October's HRA was already advanced — the revision is the difference."""
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        monthly, _vac = self._confirmed_monthly_before_the_advance(
            leave, date(2026, 10, 1), date(2026, 10, 31))
        paid_net = self._line(monthly, 'NET')

        revision = monthly._create_revision_payslip()

        # What it should have paid: the same month without the allowance
        # the advance covered (and without the GOSI it also covered).
        self.assertEqual(self._input(revision, 'PRIOR_HRA'), self.HRA)
        self.assertEqual(self._input(revision, 'PRIOR_GOSI'), self.GOSI)
        self.assertEqual(self._line(revision, 'HRA'), 0.0)
        self.assertEqual(self._line(revision, 'GOSI'), 0.0)

        # And what was actually paid — this payslip's NET alone.
        self.assertEqual(self._input(revision, 'PRIOR_NET'), paid_net)
        self.assertEqual(self._line(revision, 'NET'), -self.HRA + self.GOSI,
                         'The difference is the allowance, less the GOSI '
                         'that came off it.')

    def test_a_revision_never_subtracts_the_vacation_payslips_net(self):
        """The two documents are not two halves of one statement.

        Each pays a full gross and deducts the days the other covers, so
        folding the vacation payslip into PRIOR_NET while the recompute
        credits only this payslip's window claws back salary that was
        legitimately paid.
        """
        leave = self._create_leave(date(2026, 9, 20), date(2026, 10, 10))
        monthly, vac = self._confirmed_monthly_before_the_advance(
            leave, date(2026, 10, 1), date(2026, 10, 31))

        prior = monthly._revision_prior_slips()

        self.assertIn(monthly, prior)
        self.assertNotIn(vac, prior)
        revision = monthly._create_revision_payslip()
        self.assertEqual(self._input(revision, 'PRIOR_NET'),
                         self._line(monthly, 'NET'))
        # Nor are its one-time inputs carried over as things this payslip
        # was supposed to pay.
        self.assertEqual(self._input(revision, 'VACATION_BAL'), 0.0)
        self.assertEqual(self._input(revision, 'VACATION_HRA'), 0.0)

    def test_an_ordinary_revision_is_unaffected(self):
        """No other document paid anything — nothing to subtract."""
        monthly = self._monthly(date(2026, 10, 1), date(2026, 10, 31))
        monthly.write({'state': 'done'})

        revision = monthly._create_revision_payslip()

        self.assertEqual(self._input(revision, 'PRIOR_HRA'), 0.0,
                         'A revision must never subtract its own HRA.')
        self.assertEqual(self._line(revision, 'HRA'), self.HRA)
        self.assertEqual(self._line(revision, 'NET'), 0.0,
                         'Nothing changed, so nothing is owed either way.')
