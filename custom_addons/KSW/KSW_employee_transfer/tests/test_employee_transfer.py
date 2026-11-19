from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged


@tagged('post_install', '-at_install')
class TestEmployeeTransfer(TransactionCase):
    """Release / request → the GM decides → HR carries it out."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Users = cls.env['res.users'].with_context(no_reset_password=True)

        def _user(login, group):
            user = Users.create({'name': login, 'login': login,
                                 'group_ids': [(6, 0, [cls.env.ref(group).id])]})
            cls.env['hr.employee'].create({'name': login, 'user_id': user.id})
            return user

        cls.manager = _user('et_manager', 'KSW_employee_transfer.group_transfer_requester')
        cls.bravo_boss = _user('et_bravo_boss', 'KSW_employee_transfer.group_transfer_requester')
        cls.hr = _user('et_hr', 'KSW_employee_transfer.group_transfer_hr')
        cls.gm = _user('et_gm', 'KSW_employee_transfer.group_transfer_gm')
        Department = cls.env['hr.department']
        cls.dept_a = Department.create({'name': 'ET Alpha', 'manager_id': cls.manager.employee_id.id})
        cls.dept_b = Department.create({'name': 'ET Bravo', 'manager_id': cls.bravo_boss.employee_id.id})
        cls.job = cls.env['hr.job'].create({'name': 'ET Driver'})
        cls.other_job = cls.env['hr.job'].create({'name': 'ET Clerk'})
        cls.employee = cls.env['hr.employee'].create({
            'name': 'ET Worker', 'department_id': cls.dept_a.id, 'job_id': cls.job.id,
            'parent_id': cls.manager.employee_id.id})
        cls.outsider = cls.env['hr.employee'].create({'name': 'ET Outsider', 'department_id': cls.dept_b.id})
        # Bravo's HR responsible is a choice someone made, not OdooBot's default.
        cls.outsider.current_version_id.hr_responsible_id = cls.hr
        cls.Request = cls.env['ksw.employee.request']

    # ------------------------------------------------------------------
    def _release(self, employees=None, user=None):
        request = self.Request.with_user(user or self.manager).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'Too many',
            'release_line_ids': [(0, 0, {'employee_id': e.id}) for e in (employees or self.employee)],
        })
        request.action_submit()
        return request

    def _ask(self, quantity=1, job=None):
        request = self.Request.with_user(self.bravo_boss).create({
            'request_type': 'addition', 'department_id': self.dept_b.id, 'reason': 'Short of drivers',
            'addition_line_ids': [(0, 0, {'job_id': (job or self.job).id, 'quantity': quantity})],
        })
        request.action_submit()
        return request

    def _decide(self, lines, decision, addition_line=False, note=False, date=False):
        wizard = self.env['ksw.employee.release.decision.wizard'].with_user(self.gm).create({
            'release_line_ids': [(6, 0, lines.ids)], 'decision': decision,
            'addition_line_id': addition_line and addition_line.id, 'note': note,
            'effective_date': date or fields.Date.today()})
        wizard.action_confirm()

    def _given(self, date=False):
        release, ask = self._release(), self._ask()
        self._decide(release.release_line_ids, 'transfer', ask.addition_line_ids, date=date)
        return release, ask, release.release_line_ids.sudo().transfer_id

    # ------------------------------------------------------------------
    def test_full_route(self):
        release, ask, transfer = self._given()
        line = release.release_line_ids
        self.assertEqual(line.status, 'transferring')
        self.assertEqual(ask.addition_line_ids.open_quantity, 0)
        self.assertEqual(transfer.state, 'hr')
        self.assertEqual(transfer.sudo().gm_user_id, self.gm)
        # Only HR completes it, and it waits for HR on Home until then.
        Transfer, mine = self.env['ksw.employee.transfer'], [('x_is_pending_my_action', '=', True)]
        self.assertIn(transfer, Transfer.with_user(self.hr).search(mine))
        self.assertNotIn(transfer, Transfer.with_user(self.gm).search(mine))
        with self.assertRaises(UserError):
            transfer.with_user(self.gm).action_hr_approve()
        transfer.with_user(self.hr).action_hr_approve()
        self.assertNotIn(transfer, Transfer.with_user(self.hr).search(mine))
        self.assertEqual(transfer.state, 'applied')
        self.assertEqual(self.employee.department_id, self.dept_b)
        # He answers to the new department now, not the old one.
        self.assertEqual(self.employee.parent_id, self.bravo_boss.employee_id)
        self.assertEqual(self.employee.leave_manager_id, self.bravo_boss)
        self.assertEqual(self.employee.attendance_manager_id, self.bravo_boss)
        self.assertEqual(self.employee.current_version_id.hr_responsible_id, self.hr)
        transfer = transfer.sudo()
        self.assertEqual(transfer.hr_user_id, self.hr)
        self.assertEqual(transfer.from_manager_id, self.manager.employee_id)
        self.assertEqual(transfer.from_leave_manager_id, self.manager)
        self.assertEqual(line.status, 'transferred')
        self.assertEqual((release.state, ask.state), ('done', 'done'))
        self.assertEqual(self.employee.with_user(self.hr).x_transfer_count, 1)

    def test_nobody_transfers_directly(self):
        for user in (self.manager, self.hr, self.gm):
            with self.assertRaises(UserError):
                self.env['ksw.employee.transfer'].with_user(user).create({
                    'employee_id': self.employee.id, 'to_department_id': self.dept_b.id, 'reason': 'x'})

    def test_transfer_needs_an_open_request(self):
        release = self._release()
        line = release.release_line_ids
        with self.assertRaises(UserError):
            self._decide(line, 'transfer')
        # Not back to his own department.
        own = self.Request.with_user(self.manager).create({
            'request_type': 'addition', 'department_id': self.dept_a.id, 'reason': 'x',
            'addition_line_ids': [(0, 0, {'job_id': self.job.id})]})
        own.action_submit()
        with self.assertRaises(UserError):
            self._decide(line, 'transfer', own.addition_line_ids)
        # No more than asked for.
        second = self.env['hr.employee'].create({'name': 'ET Second', 'department_id': self.dept_a.id,
                                                 'job_id': self.job.id})
        both = self._release(second) | release
        with self.assertRaises(UserError):
            self._decide(both.release_line_ids, 'transfer', self._ask(quantity=1).addition_line_ids)

    def test_other_job_warns_only(self):
        release, ask = self._release(), self._ask(job=self.other_job)
        wizard = self.env['ksw.employee.release.decision.wizard'].with_user(self.gm).create({
            'release_line_ids': [(6, 0, release.release_line_ids.ids)], 'decision': 'transfer',
            'addition_line_id': ask.addition_line_ids.id})
        self.assertIn('ET Clerk', wizard.job_warning)
        wizard.action_confirm()  # no note needed: the GM's call
        self.assertEqual(release.release_line_ids.status, 'transferring')

    def test_hold_then_fill_from_the_request(self):
        release = self._release()
        line = release.release_line_ids
        self._decide(line, 'hold')  # a note is optional
        self.assertEqual(line.status, 'hold')
        self.assertEqual(self.employee.department_id, self.dept_a)
        ask = self._ask()
        wizard = self.env['ksw.employee.addition.decision.wizard'].with_user(self.gm).create({
            'addition_line_id': ask.addition_line_ids.id, 'decision': 'fill',
            'release_line_ids': [(6, 0, line.ids)]})
        self.assertIn(line, wizard.candidate_ids)
        wizard.action_confirm()
        self.assertEqual(line.status, 'transferring')

    def test_refuse_release_keeps_him(self):
        release = self._release()
        self._decide(release.release_line_ids, 'keep')
        self.assertEqual(release.release_line_ids.status, 'kept')
        self.assertEqual(release.state, 'done')
        self.assertEqual(self.employee.department_id, self.dept_a)

    def test_end_of_service_waits_for_hr(self):
        if 'x_eos_leave_ids' in self.env['ksw.employee.release.line']._fields:
            self.skipTest('KSW_employee_transfer_eos opens the EOS request instead (its own test).')
        release = self._release()
        self._decide(release.release_line_ids, 'terminate')
        self.assertEqual(release.release_line_ids.status, 'terminating')
        self.assertTrue(release.x_waiting_hr)
        Request = self.Request.with_user(self.hr)
        self.assertIn(release, Request.search([('x_is_pending_my_action', '=', True)]))
        self.assertNotIn(release, self.Request.with_user(self.gm).search([('x_is_pending_my_action', '=', True)]))
        with self.assertRaises(UserError):
            release.release_line_ids.with_user(self.manager).action_hr_terminate()

    def test_hire_from_outside(self):
        ask = self._ask(quantity=2)
        line = ask.addition_line_ids
        self.env['ksw.employee.addition.decision.wizard'].with_user(self.gm).create({
            'addition_line_id': line.id, 'decision': 'external', 'quantity': 1}).action_confirm()
        self.assertEqual((line.open_quantity, line.hire_open, line.status), (1, 1, 'gm'))
        self.env['ksw.employee.addition.decision.wizard'].with_user(self.gm).create({
            'addition_line_id': line.id, 'decision': 'refuse', 'quantity': 1, 'note': 'One is enough'
        }).action_confirm()
        self.assertEqual(line.status, 'hiring')
        self.assertTrue(ask.x_waiting_hr)
        newcomer = self.env['hr.employee'].create({'name': 'ET New', 'department_id': self.dept_b.id})
        with self.assertRaises(UserError):
            line.with_user(self.hr)._ksw_record_hires(self.employee)  # not in Bravo
        self.env['ksw.employee.hire.wizard'].with_user(self.hr).create({
            'addition_line_id': line.id, 'employee_ids': [(6, 0, newcomer.ids)]}).action_confirm()
        self.assertEqual(line.status, 'done')
        self.assertEqual(ask.state, 'done')

    def test_hr_returns_to_the_gm(self):
        release, ask, transfer = self._given()
        self.env['ksw.employee.transfer.refuse.wizard'].with_user(self.hr).create(
            {'transfer_id': transfer.id, 'reason': 'Iqama under renewal'}).action_confirm()
        self.assertEqual(transfer.state, 'refused')
        line = release.release_line_ids
        self.assertEqual(line.status, 'gm')
        self.assertEqual(ask.addition_line_ids.open_quantity, 1)
        self.assertEqual(self.employee.department_id, self.dept_a)

    def test_gm_cancels_his_decision(self):
        release, ask, transfer = self._given()
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).action_cancel()
        transfer.with_user(self.gm).action_cancel()
        self.assertEqual(release.release_line_ids.status, 'gm')

    def test_waiting_for_me(self):
        draft = self.Request.with_user(self.manager).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'x',
            'release_line_ids': [(0, 0, {'employee_id': self.employee.id})]})
        mine = [('x_is_pending_my_action', '=', True)]
        self.assertIn(draft, self.Request.with_user(self.manager).search(mine))
        draft.action_submit()
        self.assertNotIn(draft, self.Request.with_user(self.manager).search(mine))
        self.assertIn(draft, self.Request.with_user(self.gm).search(mine))
        self.assertNotIn(draft, self.Request.with_user(self.hr).search(mine))
        self._decide(draft.release_line_ids, 'keep', note='No')
        self.assertNotIn(draft, self.Request.with_user(self.gm).search(mine))

    def test_department_preselected_when_only_one(self):
        Request = self.Request.with_user(self.manager)
        new = Request.new({'request_type': 'release'})
        self.assertEqual(new.department_id, self.dept_a)
        self.assertTrue(new.x_department_locked)
        # Manager of two departments: he picks, nothing is locked.
        self.env['hr.department'].create({'name': 'ET Charlie', 'manager_id': self.manager.employee_id.id})
        self.env.invalidate_all()
        new = Request.new({'request_type': 'release'})
        self.assertFalse(new.department_id)
        self.assertFalse(new.x_department_locked)
        self.assertEqual(len(new.allowed_department_ids), 2)
        # HR and the GM pick any department.
        self.assertFalse(self.Request.with_user(self.gm).new({}).x_department_locked)

    def test_requester_scope(self):
        with self.assertRaises(UserError):
            self.Request.with_user(self.manager).create({
                'request_type': 'release', 'department_id': self.dept_b.id, 'reason': 'x'})
        request = self.Request.with_user(self.manager).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'x',
            'release_line_ids': [(0, 0, {'employee_id': self.outsider.id})]})
        with self.assertRaises(UserError):
            request.action_submit()
        # Submitted lines are the GM's: the department cannot change them.
        ok = self._release()
        with self.assertRaises(UserError):
            ok.release_line_ids.with_user(self.manager).write({'note': 'changed'})
        with self.assertRaises(UserError):
            ok.release_line_ids.with_user(self.manager).write({'decision': 'keep'})

    def test_released_once_at_a_time(self):
        self._release()
        with self.assertRaises(UserError):
            self._release()

    def test_proposal_from_the_new_department(self):
        _release, _ask, transfer = self._given()
        self.assertEqual(transfer.to_manager_id, self.bravo_boss.employee_id)
        self.assertEqual(transfer.to_leave_manager_id, self.bravo_boss)
        self.assertEqual(transfer.to_hr_responsible_id, self.hr)
        # HR cannot confirm without a manager.
        transfer.with_user(self.hr).to_manager_id = False
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).action_hr_approve()
        # Who moves where is the GM's decision, not HR's to edit.
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).write({'to_department_id': self.dept_a.id})

    def test_future_date_waits_for_the_cron(self):
        tomorrow = fields.Date.today() + timedelta(days=1)
        _release, _ask, transfer = self._given(date=tomorrow)
        transfer.with_user(self.hr).action_hr_approve()
        self.assertEqual(transfer.state, 'approved')
        self.assertEqual(self.employee.department_id, self.dept_a)
        transfer.sudo().effective_date = fields.Date.today()
        self.env['ksw.employee.transfer']._cron_apply_due()
        self.assertEqual(transfer.state, 'applied')

    def test_history_kept_in_versions(self):
        old_version = self.employee.current_version_id
        _release, _ask, transfer = self._given(date=fields.Date.today() + timedelta(days=3))
        transfer.with_user(self.hr).action_hr_approve()
        transfer.sudo()._apply()
        self.assertEqual(old_version.department_id, self.dept_a)
        self.assertNotEqual(old_version.hr_responsible_id, self.hr)
        self.assertNotEqual(transfer.sudo().version_id, old_version)

    def test_hr_without_hr_rights_applies_a_dated_transfer(self):
        """A new contract version on another date, confirmed by a transfer HR
        user with no Employees rights: hr.employee.create_version would refuse."""
        yesterday = fields.Date.today() - timedelta(days=1)
        _release, _ask, transfer = self._given(date=yesterday)
        self.assertFalse(self.hr.has_group('hr.group_hr_user'))
        transfer.with_user(self.hr).action_hr_approve()
        self.assertEqual(transfer.state, 'applied')
        self.assertEqual(transfer.sudo().version_id.date_version, yesterday)
        # The requester still reads his release (the version field is HR's).
        transfer.with_user(self.bravo_boss).web_read({'name': {}, 'state': {}})

    def test_step_stamps_cannot_be_forged(self):
        _release, _ask, transfer = self._given()
        with self.assertRaises(UserError):
            transfer.with_user(self.hr).write({'hr_user_id': self.hr.id})
        release = self.Request.with_user(self.manager).create({
            'request_type': 'release', 'department_id': self.dept_a.id, 'reason': 'x', 'state': 'done'})
        self.assertEqual(release.state, 'draft')
