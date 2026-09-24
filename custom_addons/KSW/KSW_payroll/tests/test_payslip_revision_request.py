# -*- coding: utf-8 -*-
"""Employee-raised payslip revision requests.

The revision itself already existed: a payroll officer opens a confirmed
payslip and presses "Issue Revision".  What did not exist was the *route
in*.  In practice the complaint starts with the employee, who brings a
signed form to the HR desk; nothing recorded that it had been brought, who
checked it, what they found, or who authorised paying the difference.

    draft → pending_hr → pending_gm → pending_acc → paid

Covered here:
  1. Filing — who may, against what, and the header derived from the payslip.
  2. Submission and the HR step, including the mandatory findings note.
  3. The GM step — scoped to the requester's own department GM.
  4. Return to HR, which cancels the revision rather than editing it.
  5. The accounting step — payment method, and confirming the revision.
  6. Refusal at each step, with a reason.
  7. The field whitelist: write access on the document is not permission
     to walk the chain over RPC.
  8. The bank file, generated for this one employee.

Same structure fixture as `test_payslip_revision.py`, and for the same
reason: ATTDED is deliberately left out, because the test employee has no
attendance and every calendar day would otherwise be deducted as
unpresented, squeezing the net to zero and making every figure here
unpredictable.
"""
from datetime import date

from odoo.exceptions import UserError, AccessError
from odoo.tests.common import TransactionCase


class TestPayslipRevisionRequest(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        Rule = cls.env['hr.salary.rule'].sudo()
        rules = Rule.browse()
        for code in ['BASIC', 'HRA', 'GROSS', 'GOSI',
                     'ADDITIONAL_COMMISSIONS', 'KSW_DEDUCTIONS',
                     'PRIOR_NET', 'NET']:
            rule = Rule.search([('code', '=', code)], limit=1)
            if not rule:
                raise AssertionError('salary rule %s is missing' % code)
            rules |= rule
        cls.structure = cls.env['hr.payroll.structure'].sudo().create({
            'name': 'Revision Request Test Structure',
            'code': 'REVREQTEST',
            'rule_ids': [(6, 0, rules.ids)],
        })

        cls.calendar = cls.env['resource.calendar'].create({
            'name': 'Revision Request Calendar',
            'tz': 'Asia/Riyadh',
        })

        group_user = cls.env.ref('base.group_user')
        group_officer = cls.env.ref('om_hr_payroll.group_hr_payroll_user')
        group_self = cls.env.ref('KSW_payroll.group_hr_payroll_self')
        group_hr_leave = cls.env.ref('KSW_annual_leave.group_annual_leave_hr')
        group_gm = cls.env.ref('KSW_annual_leave.group_annual_leave_gm')
        group_acc = cls.env.ref('KSW_annual_leave.group_annual_leave_acc')
        # See test_payslip_revision.py: confirming ANY payslip needs this,
        # because the salary rules read `contract.wage` inside safe_eval as
        # the calling user and `wage` is manager-gated (pitfall #5).
        group_hr_manager = cls.env.ref('hr.group_hr_manager')

        def _user(name, login, groups):
            return cls.env['res.users'].create({
                'name': name, 'login': login,
                'email': '%s@revreq.test' % login,
                'group_ids': [(6, 0, [g.id for g in groups])],
            })

        cls.user_employee = _user('Revreq Employee', 'revreq_employee',
                                  [group_user, group_self])
        cls.user_manager = _user('Revreq Manager', 'revreq_manager',
                                 [group_user, group_self])
        cls.user_hr = _user('Revreq HR', 'revreq_hr',
                            [group_user, group_officer, group_hr_manager])
        # The HR desk the employee actually brings the form to: the HR
        # Approver of the annual-leave chain, who holds no payroll tier at
        # all.  Deliberately given nothing else — this user is the proof
        # that the HR step needs no hr.payslip access of its own.
        cls.user_hr_leave = _user('Revreq HR Approver', 'revreq_hr_leave',
                                  [group_user, group_hr_leave])
        cls.user_gm = _user('Revreq GM', 'revreq_gm', [group_user, group_gm])
        cls.user_other_gm = _user('Revreq Other GM', 'revreq_other_gm',
                                  [group_user, group_gm])
        cls.user_acc = _user('Revreq Accounting', 'revreq_acc',
                             [group_user, group_acc])
        cls.user_outsider = _user('Revreq Outsider', 'revreq_outsider',
                                  [group_user, group_self])

        Employee = cls.env['hr.employee'].sudo()
        cls.gm_employee = Employee.create({
            'name': 'Revreq GM Employee', 'user_id': cls.user_gm.id})
        cls.department = cls.env['hr.department'].sudo().create({
            'name': 'Revreq Department', 'x_gm_id': cls.gm_employee.id})

        cls.manager = Employee.create({
            'name': 'Revreq Manager Employee',
            'user_id': cls.user_manager.id,
            'department_id': cls.department.id,
        })
        cls.employee = Employee.create({
            'name': 'Revreq Test Employee',
            'user_id': cls.user_employee.id,
            'parent_id': cls.manager.id,
            'department_id': cls.department.id,
            'resource_calendar_id': cls.calendar.id,
        })
        cls.outsider = Employee.create({
            'name': 'Revreq Outsider Employee',
            'user_id': cls.user_outsider.id,
        })

        cls.version = cls.employee.current_version_id
        cls.version.write({
            'name': 'Revreq Version',
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'resource_calendar_id': cls.calendar.id,
            'wage': 5000.0, 'hra': 1000.0, 'da': 0.0,
            'travel_allowance': 0.0, 'mobile_allowance': 0.0,
            'meal_allowance': 0.0, 'medical_allowance': 0.0,
            'other_allowance': 0.0,
            'struct_id': cls.structure.id,
        })
        cls.employee._compute_current_version_id()

        cls.month_start = date(2026, 7, 1)
        cls.month_end = date(2026, 7, 31)
        cls.Request = cls.env['ksw.payslip.revision.request']
        cls.Wizard = cls.env['ksw.revision.request.reason.wizard']

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_done_payslip(self, name='Original'):
        slip = self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': name,
            'date_from': self.month_start,
            'date_to': self.month_end,
            'struct_id': self.structure.id,
            'version_id': self.version.id,
        })
        slip.action_payslip_done()
        return slip

    def _file(self, slip=None, user=None, reason='Overtime is missing.'):
        slip = slip or self._make_done_payslip()
        user = user or self.user_employee
        return self.Request.with_user(user).create({
            'payslip_id': slip.id,
            'reason': reason,
        })

    def _submitted(self, slip=None):
        req = self._file(slip)
        req.with_user(self.user_employee).action_submit()
        return req

    def _accepted(self, slip=None):
        """Walk to pending_gm with a revision issued."""
        req = self._submitted(slip)
        req.with_user(self.user_hr).write({'hr_comment': 'Complaint holds.'})
        req.with_user(self.user_hr).action_hr_accept()
        return req

    def _make_payable(self, req, amount=800.0):
        """Give the revision a positive difference to approve and pay."""
        revision = req.revision_payslip_id.sudo()
        self.env['hr.payslip.input'].sudo().create({
            'payslip_id': revision.id,
            'version_id': revision.version_id.id,
            'name': 'Missing overtime',
            'code': 'ADDITIONAL_COMMISSIONS',
            'amount': amount,
        })
        revision.compute_sheet()
        req.invalidate_recordset()
        return req

    def _at_accounting(self, slip=None, amount=800.0):
        req = self._make_payable(self._accepted(slip), amount)
        req.with_user(self.user_gm).action_gm_approve()
        return req

    # ==================================================================
    # 1. Filing
    # ==================================================================

    def test_employee_files_against_own_payslip(self):
        slip = self._make_done_payslip()
        req = self._file(slip)
        self.assertEqual(req.state, 'draft')
        self.assertEqual(req.employee_id, self.employee)
        self.assertEqual(req.department_id, self.department)
        self.assertEqual(req.date_from, self.month_start)
        self.assertEqual(req.date_to, self.month_end)
        self.assertTrue(req.name.startswith('REVREQ/'))

    def test_the_form_fills_the_header_before_it_is_saved(self):
        """Opening the form from a payslip must show Employee, Department,
        Period and Originally Paid immediately.

        These are plain stored fields written by create() — they have to
        survive the payslip moving on (pitfall #50) — so nothing fills them
        on screen unless an onchange does, and ``employee_id`` is
        ``required``: an empty one is not merely blank, the client refuses
        to save at all.
        """
        slip = self._make_done_payslip()
        draft = self.Request.with_user(self.user_employee).new({
            'payslip_id': slip.id})
        draft._onchange_payslip_id()
        self.assertEqual(draft.employee_id, self.employee)
        self.assertEqual(draft.department_id, self.department)
        self.assertEqual(draft.date_from, self.month_start)
        self.assertEqual(draft.date_to, self.month_end)
        self.assertTrue(draft.original_net)

    def test_a_brand_new_form_is_editable(self):
        """``can_edit`` drives the payslip picker's readonly=. On an unsaved
        record ``employee_id`` is still empty, so a check that reads it
        alone opens the form read-only and there is no way to pick a
        payslip at all."""
        Req = self.Request
        self.assertTrue(Req.with_user(self.user_employee).new({}).can_edit)
        self.assertTrue(Req.with_user(self.user_hr).new({}).can_edit)

    def test_the_picker_closes_for_someone_elses_payslip(self):
        """The fallback above must not hand an outsider an editable draft:
        ``can_edit`` falls back to the *payslip's* employee, not to 'yes'."""
        slip = self._make_done_payslip()
        draft = self.Request.with_user(self.user_outsider).new({
            'payslip_id': slip.id})
        self.assertFalse(draft.can_edit)

    def test_header_is_stamped_not_related(self):
        """The request must keep stating which employee and period it was
        about even if the payslip is later repointed (pitfall #50)."""
        req = self._file()
        req.payslip_id.sudo().write({'employee_id': self.outsider.id})
        req.invalidate_recordset()
        self.assertEqual(req.employee_id, self.employee)

    def test_direct_manager_may_file_for_the_team(self):
        slip = self._make_done_payslip()
        req = self._file(slip, user=self.user_manager)
        self.assertEqual(req.employee_id, self.employee)

    def test_hr_may_file_on_behalf(self):
        slip = self._make_done_payslip()
        req = self._file(slip, user=self.user_hr)
        self.assertEqual(req.employee_id, self.employee)

    def test_outsider_may_not_file(self):
        slip = self._make_done_payslip()
        with self.assertRaises(UserError):
            self._file(slip, user=self.user_outsider)

    def test_draft_payslip_cannot_be_disputed(self):
        slip = self.env['hr.payslip'].sudo().create({
            'employee_id': self.employee.id,
            'name': 'Still draft',
            'date_from': self.month_start,
            'date_to': self.month_end,
            'struct_id': self.structure.id,
            'version_id': self.version.id,
        })
        with self.assertRaises(UserError):
            self._file(slip)

    def test_only_one_open_request_per_payslip(self):
        slip = self._make_done_payslip()
        self._file(slip)
        with self.assertRaises(UserError):
            self._file(slip)

    def test_a_refused_request_frees_the_payslip(self):
        slip = self._make_done_payslip()
        req = self._submitted(slip)
        self.Wizard.with_user(self.user_hr).create({
            'request_id': req.id, 'mode': 'refuse',
            'reason': 'Already paid in June.'}).action_confirm()
        self.assertEqual(req.state, 'refused')
        # A second complaint about the same payslip is now allowed.
        self._file(slip)

    # ==================================================================
    # 2. Submission and the HR step
    # ==================================================================

    def test_submit_moves_to_hr_and_notifies(self):
        req = self._file()
        hr_partners = self.env.ref(
            'om_hr_payroll.group_hr_payroll_user'
        ).sudo().user_ids.mapped('partner_id')
        existing = req.sudo().message_ids.ids
        req.with_user(self.user_employee).action_submit()
        self.assertEqual(req.state, 'pending_hr')
        new = req.sudo().message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(new.filtered(
            lambda m: self.user_hr.partner_id in m.partner_ids),
            'HR was not notified of the new request')

    def test_hr_accept_requires_findings(self):
        req = self._submitted()
        with self.assertRaises(UserError):
            req.with_user(self.user_hr).action_hr_accept()
        self.assertEqual(req.state, 'pending_hr')

    def test_hr_accept_issues_the_revision(self):
        req = self._accepted()
        self.assertEqual(req.state, 'pending_gm')
        revision = req.revision_payslip_id.sudo()
        self.assertTrue(revision)
        self.assertTrue(revision.x_is_revision)
        self.assertEqual(revision.x_revised_payslip_id, req.payslip_id)
        self.assertEqual(
            revision.state, 'draft',
            'the revision must stay draft so the GM approves a computed '
            'figure and accounting can still recompute before paying')
        self.assertEqual(req.hr_user_id, self.user_hr)

    def test_only_hr_may_accept(self):
        req = self._submitted()
        req.sudo().write({'hr_comment': 'x'})
        for user in (self.user_employee, self.user_gm, self.user_acc):
            with self.assertRaises(UserError):
                req.with_user(user).action_hr_accept()

    # ------------------------------------------------------------------
    # The second HR role: the annual-leave HR Approver
    # ------------------------------------------------------------------

    def test_annual_leave_hr_approver_is_hr_here_too(self):
        """The HR desk that reviews leave reviews salary complaints.

        This user holds no payroll tier: no hr.payslip access, no batches,
        no salary rules.  Everything the step touches on the payslip goes
        through sudo() after authority has been checked, so the whole HR
        step must work on that group alone.
        """
        self.assertFalse(self.user_hr_leave.has_group(
            'om_hr_payroll.group_hr_payroll_user'))
        req = self._submitted()
        self.assertTrue(req.with_user(self.user_hr_leave).can_hr_act)
        req.with_user(self.user_hr_leave).write(
            {'hr_comment': 'Checked: one absence was deducted wrongly.'})
        req.with_user(self.user_hr_leave).action_hr_accept()
        self.assertEqual(req.state, 'pending_gm')
        self.assertEqual(req.hr_user_id, self.user_hr_leave)
        self.assertTrue(req.revision_payslip_id.sudo().x_is_revision)

    def test_annual_leave_hr_approver_may_refuse(self):
        req = self._submitted()
        wizard = self.Wizard.with_user(self.user_hr_leave).create({
            'request_id': req.id, 'mode': 'refuse',
            'reason': 'The overtime was already paid in June.',
        })
        wizard.action_confirm()
        self.assertEqual(req.state, 'refused')
        self.assertEqual(req.refused_by_id, self.user_hr_leave)

    def test_annual_leave_hr_approver_sees_it_waiting(self):
        """Acting on a step and being told to act on it are one decision.

        The button guard and the "Waiting for My Action" filter read the
        same predicate, so a role can never hold one without the other.
        """
        req = self._submitted()
        self.assertTrue(
            req.with_user(self.user_hr_leave).is_pending_my_action)
        self.assertIn(req, self.Request.with_user(self.user_hr_leave).search(
            [('is_pending_my_action', '=', True)]))

    def test_annual_leave_hr_approver_is_notified_on_submission(self):
        req = self._file()
        existing = req.sudo().message_ids.ids
        req.with_user(self.user_employee).action_submit()
        new = req.sudo().message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(new.filtered(
            lambda m: self.user_hr_leave.partner_id in m.partner_ids),
            'the HR Approver was not notified of the new request')

    def test_annual_leave_hr_approver_gets_no_payslip_access(self):
        """Being HR for this document is not being a payroll officer."""
        slip = self._make_done_payslip()
        with self.assertRaises(AccessError):
            slip.with_user(self.user_hr_leave).read(['number'])

    # ==================================================================
    # 3. The GM step
    # ==================================================================

    def test_gm_cannot_act_before_hr(self):
        req = self._submitted()
        with self.assertRaises(UserError):
            req.with_user(self.user_gm).action_gm_approve()

    def test_only_the_department_gm_may_approve(self):
        req = self._make_payable(self._accepted())
        with self.assertRaises(UserError):
            req.with_user(self.user_other_gm).action_gm_approve()
        req.with_user(self.user_gm).action_gm_approve()
        self.assertEqual(req.state, 'pending_acc')
        self.assertEqual(req.gm_user_id, self.user_gm)

    def test_a_gm_of_another_department_cannot_even_see_it(self):
        """Authority and scope are different systems — the record rule has
        to follow x_effective_gm_id too (pitfall #38)."""
        req = self._accepted()
        self.assertFalse(
            self.Request.with_user(self.user_other_gm).search(
                [('id', '=', req.id)]))
        self.assertTrue(
            self.Request.with_user(self.user_gm).search(
                [('id', '=', req.id)]))

    def test_gm_cannot_approve_a_zero_difference(self):
        """Nothing changed, so nothing is owed — that is a refusal, not an
        approved payment of zero."""
        req = self._accepted()
        self.assertEqual(req.difference_amount, 0.0)
        with self.assertRaises(UserError):
            req.with_user(self.user_gm).action_gm_approve()

    def test_gm_return_sends_it_back_and_drops_the_revision(self):
        req = self._make_payable(self._accepted())
        revision = req.revision_payslip_id.sudo()
        self.Wizard.with_user(self.user_gm).create({
            'request_id': req.id, 'mode': 'return',
            'reason': '14 July attendance is still missing.'}).action_confirm()
        self.assertEqual(req.state, 'pending_hr')
        self.assertFalse(req.revision_payslip_id)
        self.assertEqual(revision.state, 'cancel')
        self.assertFalse(req.gm_user_id)

    def test_hr_can_reissue_after_a_return(self):
        req = self._make_payable(self._accepted())
        self.Wizard.with_user(self.user_gm).create({
            'request_id': req.id, 'mode': 'return',
            'reason': 'Redo it.'}).action_confirm()
        req.with_user(self.user_hr).write({'hr_comment': 'Reloaded.'})
        req.with_user(self.user_hr).action_hr_accept()
        self.assertEqual(req.state, 'pending_gm')
        self.assertTrue(req.revision_payslip_id)

    # ==================================================================
    # 4. The accounting step
    # ==================================================================

    def test_payment_method_is_required(self):
        req = self._at_accounting()
        with self.assertRaises(UserError):
            req.with_user(self.user_acc).action_acc_pay()
        self.assertEqual(req.state, 'pending_acc')

    def test_accounting_pays_and_confirms_the_revision(self):
        req = self._at_accounting()
        req.with_user(self.user_acc).write({'payment_method': 'cash'})
        req.with_user(self.user_acc).action_acc_pay()
        self.assertEqual(req.state, 'paid')
        self.assertEqual(req.revision_payslip_id.sudo().state, 'done')
        self.assertEqual(req.acc_user_id, self.user_acc)
        self.assertTrue(req.payment_date)

    def test_only_accounting_may_pay(self):
        req = self._at_accounting()
        req.sudo().write({'payment_method': 'cash'})
        for user in (self.user_employee, self.user_gm, self.user_hr):
            with self.assertRaises(UserError):
                req.with_user(user).action_acc_pay()

    def test_employee_is_notified_when_paid(self):
        req = self._at_accounting()
        req.sudo().write({'payment_method': 'bank'})
        existing = req.sudo().message_ids.ids
        req.with_user(self.user_acc).action_acc_pay()
        new = req.sudo().message_ids.filtered(lambda m: m.id not in existing)
        self.assertTrue(new.filtered(
            lambda m: self.user_employee.partner_id in m.partner_ids))

    # ==================================================================
    # 5. Refusal and cancellation
    # ==================================================================

    def test_refusal_records_the_reason_and_cancels_the_revision(self):
        req = self._make_payable(self._accepted())
        revision = req.revision_payslip_id.sudo()
        self.Wizard.with_user(self.user_gm).create({
            'request_id': req.id, 'mode': 'refuse',
            'reason': 'The overtime was never authorised.'}).action_confirm()
        self.assertEqual(req.state, 'refused')
        self.assertIn('never authorised', req.refuse_reason)
        self.assertEqual(req.refused_by_id, self.user_gm)
        self.assertEqual(revision.state, 'cancel')

    def test_employee_cannot_refuse_their_own_request(self):
        # Odoo 19's assertRaises does not accept a tuple of exceptions
        # (pitfall #9), and either answer is correct here: the employee is
        # stopped by the wizard's ACL or by the guard behind it.
        req = self._submitted()
        try:
            self.Wizard.with_user(self.user_employee).create({
                'request_id': req.id, 'mode': 'refuse',
                'reason': 'let me off'}).action_confirm()
        except (UserError, AccessError):
            pass
        else:
            self.fail('the employee refused their own request')
        self.assertEqual(req.state, 'pending_hr')

    def test_reopening_cannot_produce_a_second_open_request(self):
        """A refused request can be reopened — unless somebody has filed a
        fresh complaint about the same payslip in the meantime."""
        slip = self._make_done_payslip()
        first = self._submitted(slip)
        self.Wizard.with_user(self.user_hr).create({
            'request_id': first.id, 'mode': 'refuse',
            'reason': 'Not justified.'}).action_confirm()
        second = self._file(slip)
        self.assertTrue(second)
        with self.assertRaises(UserError):
            first.with_user(self.user_employee).action_reset_to_draft()
        self.assertEqual(first.state, 'refused')

    def test_a_refused_request_can_be_reopened(self):
        req = self._submitted()
        self.Wizard.with_user(self.user_hr).create({
            'request_id': req.id, 'mode': 'refuse',
            'reason': 'Missing paperwork.'}).action_confirm()
        req.with_user(self.user_employee).action_reset_to_draft()
        self.assertEqual(req.state, 'draft')
        self.assertFalse(req.refuse_reason)

    def test_employee_cancels_before_hr_acts(self):
        req = self._submitted()
        req.with_user(self.user_employee).action_cancel()
        self.assertEqual(req.state, 'cancelled')

    def test_employee_cannot_cancel_once_hr_has_acted(self):
        req = self._accepted()
        with self.assertRaises(UserError):
            req.with_user(self.user_employee).action_cancel()

    # ==================================================================
    # 6. The field whitelist
    # ==================================================================

    def test_employee_cannot_walk_the_chain_over_rpc(self):
        """View-level invisible= is cosmetic; write access on the document
        must not be permission to set the state (pitfall #15)."""
        req = self._submitted()
        with self.assertRaises(UserError):
            req.with_user(self.user_employee).write({'state': 'paid'})
        self.assertEqual(req.state, 'pending_hr')

    def test_employee_cannot_edit_after_submitting(self):
        req = self._submitted()
        with self.assertRaises(UserError):
            req.with_user(self.user_employee).write({'reason': 'changed'})

    def test_employee_may_edit_their_own_draft(self):
        req = self._file()
        req.with_user(self.user_employee).write({
            'reason': 'Rewritten', 'claimed_amount': 120.0})
        self.assertEqual(req.reason, 'Rewritten')

    def test_hr_findings_are_writable_only_at_the_hr_step(self):
        req = self._submitted()
        req.with_user(self.user_hr).write({'hr_comment': 'Checking.'})
        self.assertEqual(req.hr_comment, 'Checking.')
        req = self._at_accounting()
        with self.assertRaises(UserError):
            req.with_user(self.user_hr).write({'hr_comment': 'too late'})

    def test_accounting_may_set_the_payment_method_but_not_the_state(self):
        req = self._at_accounting()
        req.with_user(self.user_acc).write({
            'payment_method': 'bank', 'payment_reference': 'TRF-1'})
        self.assertEqual(req.payment_method, 'bank')
        with self.assertRaises(UserError):
            req.with_user(self.user_acc).write({'state': 'paid'})

    def test_only_a_draft_may_be_deleted(self):
        req = self._submitted()
        with self.assertRaises(UserError):
            req.with_user(self.user_employee).unlink()

    # ==================================================================
    # 7. "Waiting for me"
    # ==================================================================

    def test_pending_my_action_per_user(self):
        req = self._submitted()
        self.assertTrue(req.with_user(self.user_hr).is_pending_my_action)
        self.assertFalse(req.with_user(self.user_gm).is_pending_my_action)
        self.assertFalse(req.with_user(self.user_acc).is_pending_my_action)

    def test_pending_my_action_search_both_directions(self):
        """Odoo 19 rewrites =/!= on a boolean into in/not in before the
        search= method is called, and returning [] would match every row
        (pitfall #24)."""
        req = self._submitted()
        hits = self.Request.with_user(self.user_hr).search(
            [('is_pending_my_action', '=', True)])
        self.assertIn(req, hits)
        misses = self.Request.with_user(self.user_hr).search(
            [('is_pending_my_action', '=', False)])
        self.assertNotIn(req, misses)
        # A user nothing can ever be pending on must match nothing.
        self.assertFalse(self.Request.with_user(self.user_employee).search(
            [('is_pending_my_action', '=', True)]))

    # ==================================================================
    # 8. The bank file
    # ==================================================================

    def test_bank_file_covers_this_employee_only(self):
        req = self._at_accounting()
        req.with_user(self.user_acc).write({'payment_method': 'bank'})
        req.with_user(self.user_acc).action_acc_pay()

        # A second, unrelated employee is confirmed for the same month, to
        # prove the file is scoped to the request and not to the period.
        other_version = self.outsider.current_version_id
        other_version.write({
            'date_version': date(2025, 1, 1),
            'contract_date_start': date(2025, 1, 1),
            'wage': 4000.0, 'struct_id': self.structure.id,
        })
        self.env['hr.payslip'].sudo().create({
            'employee_id': self.outsider.id,
            'name': 'Someone else',
            'date_from': self.month_start,
            'date_to': self.month_end,
            'struct_id': self.structure.id,
            'version_id': other_version.id,
        }).action_payslip_done()

        run = req.sudo()._ensure_payment_batch()
        self.assertEqual(len(run.slip_ids), 1)
        self.assertEqual(run.slip_ids.employee_id, self.employee)
        self.assertEqual(run.slip_ids, req.revision_payslip_id.sudo())

    def test_bank_file_is_refused_before_the_revision_is_confirmed(self):
        """Confirming recomputes, so a file exported from a draft can stop
        matching what is finally paid."""
        req = self._at_accounting()
        req.with_user(self.user_acc).write({'payment_method': 'bank'})
        with self.assertRaises(UserError):
            req.with_user(self.user_acc).action_export_bank_excel()

    def test_bank_file_is_refused_before_gm_approval(self):
        req = self._accepted()
        with self.assertRaises(UserError):
            req.with_user(self.user_acc).action_export_bank_excel()
