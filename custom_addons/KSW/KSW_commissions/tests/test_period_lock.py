"""The period lock — one test per route out of an approved month.

A finalised record has more than one way out, and guarding the button you were
shown is not locking it (CLAUDE.md gotcha #37). Each test here is one row of
the audit table: if someone adds a mutation path and forgets the guard, the
matching test is what should fail.
"""
from odoo.exceptions import UserError
from odoo.tests.common import TransactionCase


class TestPeriodLock(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls.period = '2028-10-01'
        cls.dept = env['hr.department'].create({'name': 'Lock Dept'})
        # Approval and reopening both key on a GM now: the department's own
        # for approving, the company's for reopening and locking. One person
        # holds both here, which is the shape these lock tests assume.
        cls.gm = env['res.users'].sudo().create({
            'name': 'lock_dept_gm', 'login': 'lock_dept_gm',
            'group_ids': [(6, 0, [env.ref('base.group_user').id])],
        })
        cls.gm_employee = env['hr.employee'].sudo().create({
            'name': 'Lock GM Emp', 'user_id': cls.gm.id})
        cls.dept.sudo().write({'x_gm_id': cls.gm_employee.id})
        env.company.sudo().x_default_gm_id = cls.gm_employee.id

        cls.emp = env['hr.employee'].sudo().create({
            'name': 'Lock Emp', 'department_id': cls.dept.id,
        })
        if cls.emp.current_version_id:
            cls.emp.current_version_id.sudo().write({'wage': 7200.0})
        cls.component = env.ref('KSW_commissions.pay_component_overtime')

        # An officer: full ORM reach, no GM rights. The GM is exempt from the
        # lock because they are the one who can reopen it.
        cls.officer = env['res.users'].sudo().create({
            'name': 'lock_officer', 'login': 'lock_officer',
            'group_ids': [(6, 0, [
                env.ref('base.group_user').id,
                env.ref('KSW_commissions.group_commission_officer').id,
            ])],
        })

    def _batch(self, period=None):
        return self.env['ksw.pay.batch'].sudo().create({
            'component_id': self.component.id,
            'department_id': self.dept.id,
            'period': period or self.period,
        })

    def _entry(self, batch, **kwargs):
        vals = {
            'batch_id': batch.id, 'employee_id': self.emp.id,
            'date': batch.period, 'quantity': 4.0, 'reason': 'probe',
        }
        vals.update(kwargs)
        return self.env['ksw.pay.entry'].sudo().create(vals)

    def _lock_the_period(self):
        """Build and approve a run so the period becomes locked.

        The month exists as soon as the first batch does, and the department
        hands itself over — the run is never submitted wholesale by anyone.
        """
        batch = self._batch()
        self._entry(batch)
        batch.submission_id.sudo().action_submit()
        run = batch.submission_id.run_id
        # Approve as the department's GM: approval is per department now, so
        # the acting user has to be one.
        run.with_user(self.gm).sudo().action_approve()
        return run, batch

    # ------------------------------------------------------------------
    def test_00_predicate(self):
        Run = self.env['ksw.pay.run']
        self.assertFalse(Run._period_is_locked(self.period)
                         if hasattr(Run, '_period_is_locked') else False)
        from odoo.addons.KSW_commissions.models.ksw_commission_lock import (
            period_is_locked)
        self.assertFalse(period_is_locked(self.env, self.period))
        self._lock_the_period()
        self.assertTrue(period_is_locked(self.env, self.period))
        # Normalised to the first of the month.
        self.assertTrue(period_is_locked(self.env, '2028-10-17'))
        self.assertFalse(period_is_locked(self.env, '2028-11-01'))

    # -- batch routes ---------------------------------------------------
    def test_01_cannot_create_a_batch(self):
        self._lock_the_period()
        other = self.env.ref('KSW_commissions.pay_component_meals')
        with self.assertRaises(UserError):
            self.env['ksw.pay.batch'].with_user(self.officer).create({
                'component_id': other.id, 'department_id': self.dept.id,
                'period': self.period,
            })

    def test_02_cannot_write_a_batch(self):
        _run, batch = self._lock_the_period()
        with self.assertRaises(UserError):
            batch.with_user(self.officer).write({'note': 'nope'})

    def test_03_cannot_unlink_a_batch(self):
        _run, batch = self._lock_the_period()
        with self.assertRaises(UserError):
            batch.with_user(self.officer).unlink()

    def test_04_cannot_reset_a_batch(self):
        _run, batch = self._lock_the_period()
        with self.assertRaises(UserError):
            batch.with_user(self.officer).action_reset_to_draft()

    # -- entry routes ---------------------------------------------------
    def test_05_cannot_add_an_entry(self):
        _run, batch = self._lock_the_period()
        with self.assertRaises(UserError):
            self.env['ksw.pay.entry'].with_user(self.officer).create({
                'batch_id': batch.id, 'employee_id': self.emp.id,
                'date': self.period, 'quantity': 1.0, 'reason': 'late',
            })

    def test_06_cannot_edit_or_delete_an_entry(self):
        _run, batch = self._lock_the_period()
        entry = batch.entry_ids[0]
        with self.assertRaises(UserError):
            entry.with_user(self.officer).write({'quantity': 9.0})
        with self.assertRaises(UserError):
            entry.with_user(self.officer).unlink()

    def test_07_a_submitted_batch_is_closed_even_before_the_lock(self):
        """The state guard is separate from the period lock."""
        batch = self._batch(period='2028-12-01')
        entry = self._entry(batch)
        batch.action_submit()
        with self.assertRaises(UserError):
            entry.with_user(self.officer).write({'quantity': 9.0})

    # -- run routes -----------------------------------------------------
    def test_08_cannot_change_an_approved_run(self):
        run, _batch = self._lock_the_period()
        with self.assertRaises(UserError):
            run.with_user(self.officer).write({'period': '2028-11-01'})

    def test_09_cannot_delete_an_approved_run(self):
        run, _batch = self._lock_the_period()
        with self.assertRaises(UserError):
            run.with_user(self.officer).unlink()

    def test_10_only_the_gm_reopens(self):
        run, _batch = self._lock_the_period()
        with self.assertRaises(UserError):
            run.with_user(self.officer).action_reopen()

    # -- the GM is exempt, and reopening unlocks everything -------------
    def test_11_reopening_unlocks_every_route(self):
        run, batch = self._lock_the_period()
        with self.assertRaises(UserError):
            batch.with_user(self.officer).action_reset_to_draft()
        run.sudo().action_reopen()

        # Reopening lifts the period lock, but the department is still handed
        # over — that is a separate freeze, and withdrawing it is a separate
        # act. It has to be taken back before the batch opens up again.
        with self.assertRaises(UserError):
            batch.with_user(self.officer).action_reset_to_draft()
        batch.submission_id.with_user(self.officer).action_reset_to_draft()

        batch.with_user(self.officer).action_reset_to_draft()
        self.assertEqual(batch.state, 'draft')
        batch.with_user(self.officer).write({'note': 'fine now'})
        self.assertEqual(batch.note, 'fine now')

    def test_12_gm_is_not_blocked_by_the_lock(self):
        # self.gm is the department's GM, which is what grants the reach —
        # holding group_commission_gm on its own no longer does.
        _run, batch = self._lock_the_period()
        batch.with_user(self.gm).write({'note': 'GM correction'})
        self.assertEqual(batch.note, 'GM correction')
