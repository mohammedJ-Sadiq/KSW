"""Tests for the Vacation Extension request.

Covers:
  - Chain:      all six steps run, exactly as for any other KSW vacation
  - Link:       the start date is forced onto the day after the vacation ends
  - Fees:       the passport tick box is worth a fixed 120, the total sums
  - Rights:     only HR fills the fees, only Accounting the note, each at
                its own step
  - Payslip:    an extension produces none; the complement still does
  - Return:     the extension opens its own gate and takes the vacation's
                over, gives it back when reversed, and closes both on the
                real return date
  - Payroll:    the extension, not the vacation, is what blocks the batch
  - Unpaid:     the extension's days are not covered, so they still deduct
"""
import base64
from datetime import date, timedelta
from unittest.mock import patch

from odoo.addons.KSW_payroll.models.hr_leave import HrLeave as PayrollHrLeave
from odoo.exceptions import UserError, ValidationError
from odoo.tests.common import TransactionCase


class TestLeaveExtension(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': '%s@ext.test' % login,
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Ext DM', 'ext_dm')
        cls.user_hr = _mkuser(
            'Ext HR', 'ext_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser(
            'Ext Acc', 'ext_acc',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm = _mkuser(
            'Ext GM', 'ext_gm',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])

        cls.emp_dm = cls.env['hr.employee'].create(
            {'name': 'Ext DM Emp', 'user_id': cls.user_dm.id})
        cls.emp_gm = cls.env['hr.employee'].create(
            {'name': 'Ext GM Emp', 'user_id': cls.user_gm.id})

        # The GM steps follow the employee's department, not the GM group.
        cls.department = cls.env['hr.department'].create({
            'name': 'Extension Test Dept',
            'x_gm_id': cls.emp_gm.id,
        })
        cls.employee = cls.env['hr.employee'].create({
            'name': 'Extending Employee',
            'leave_manager_id': cls.user_dm.id,
            'parent_id': cls.emp_dm.id,
            'department_id': cls.department.id,
        })

        # requires_allocation must be the bool False: 'no' is a truthy string
        # and would trigger _check_validity's allocation guard.
        cls.annual_type = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave (Extension Test)',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        cls.unpaid_type = cls.env['hr.leave.type'].create({
            'name': 'Unpaid Leave (Extension Test)',
            'requires_allocation': False,
            'leave_validation_type': 'unpaid_multi',
            'is_unpaid_leave': True,
        })
        # Every real employee has a balance record; the accrual restart has
        # nothing to write to without one. Its reset date is deliberately far
        # in the past so moving it forward is the case under test.
        cls.balance = cls.env['ksw.annual.leave'].create({
            'employee_id': cls.employee.id,
            'x_opening_reset_date': date(2026, 1, 1),
        })
        cls.ext_type = cls.env['hr.leave.type'].create({
            'name': 'Vacation Extension (Extension Test)',
            'requires_allocation': False,
            'leave_validation_type': 'extension_multi',
            'is_leave_extension': True,
        })

    # ==================================================================
    # Helpers
    # ==================================================================

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _advance_to(self, leave, target_state):
        """Advance the chain to target_state, skipping steps already past."""
        steps = [
            ('pending_dm', 'action_dm_approve', self.user_dm),
            ('pending_hr', 'action_hr_approve', self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc', 'action_acc_approve', self.user_acc),
            ('pending_gm_final', 'action_gm_final_approve', self.user_gm),
        ]
        for pre_state, method, user in steps:
            current = self._STATE_RANK.get(leave.x_annual_approval_state, 0)
            if current > self._STATE_RANK.get(pre_state, 0):
                continue
            getattr(leave.with_user(user).sudo(), method)()
            if leave.x_annual_approval_state == target_state:
                break

    def _attach(self, leave):
        att = self.env['ir.attachment'].sudo().create({
            'name': 'signed_form_stub.pdf',
            'datas': base64.b64encode(b'stub'),
            'res_model': 'hr.leave',
            'res_id': leave.id,
        })
        leave.sudo().write({'x_attachment_ids': [(4, att.id)]})
        return att

    def _finalise(self, leave):
        """Walk every step, including HR's Step 6, so the leave validates."""
        self._advance_to(leave, 'pending_employee_signature')
        if leave.x_annual_approval_state == 'pending_employee_signature':
            self._attach(leave)
            leave.with_user(
                self.user_hr).sudo().action_employee_confirm_signature()
        return leave

    def _make_vacation(self, offset=0, unpaid=False, days=9):
        base = date(2027, 3, 1) + timedelta(days=offset * 60)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': (
                self.unpaid_type if unpaid else self.annual_type).id,
            'request_date_from': base,
            'request_date_to': base + timedelta(days=days),
        })

    def _make_extension(self, parent, days=4, **overrides):
        vals = {
            'employee_id': self.employee.id,
            'holiday_status_id': self.ext_type.id,
            'x_extended_leave_id': parent.id,
            'request_date_to': (
                parent.request_date_to + timedelta(days=1 + days)),
        }
        vals.update(overrides)
        return self.env['hr.leave'].sudo().create(vals)

    # ==================================================================
    # The chain
    # ==================================================================

    def test_extension_walks_all_six_steps(self):
        """An extension runs the same chain as every other KSW vacation."""
        parent = self._finalise(self._make_vacation())
        ext = self._make_extension(parent)

        self.assertEqual(ext.x_annual_approval_state, 'pending_dm')
        seen = []
        for target in ('pending_hr', 'pending_gm_initial', 'pending_acc',
                       'pending_gm_final', 'pending_employee_signature'):
            self._advance_to(ext, target)
            seen.append(ext.x_annual_approval_state)
        self.assertEqual(seen, [
            'pending_hr', 'pending_gm_initial', 'pending_acc',
            'pending_gm_final', 'pending_employee_signature',
        ])
        # Step 6 is HR's, and needs the papers.
        self._attach(ext)
        ext.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(ext.x_annual_approval_state, 'approved')
        self.assertEqual(ext.state, 'validate')

    def test_gm_final_does_not_finalise_the_extension(self):
        """GM final leaves it at HR Confirmation — the papers are still due."""
        parent = self._finalise(self._make_vacation())
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_employee_signature')
        self.assertEqual(
            ext.x_annual_approval_state, 'pending_employee_signature')
        self.assertNotEqual(ext.state, 'validate')

    # ==================================================================
    # The link and the start date
    # ==================================================================

    def test_start_date_is_forced_to_the_day_after(self):
        parent = self._finalise(self._make_vacation())
        ext = self._make_extension(parent)
        self.assertEqual(
            ext.request_date_from,
            parent.request_date_to + timedelta(days=1))

    def test_a_typed_start_date_is_overridden_on_create(self):
        """An RPC caller does not get to choose the first day."""
        parent = self._finalise(self._make_vacation())
        ext = self._make_extension(
            parent, request_date_from=parent.request_date_to + timedelta(days=9))
        self.assertEqual(
            ext.request_date_from,
            parent.request_date_to + timedelta(days=1))

    def test_moving_the_start_date_afterwards_is_refused(self):
        """The constraint, not just the create() default, is what holds."""
        parent = self._finalise(self._make_vacation())
        ext = self._make_extension(parent)
        with self.assertRaises(ValidationError):
            ext.sudo().write({
                'request_date_from': ext.request_date_from + timedelta(days=3),
            })

    def test_extension_without_a_parent_is_refused(self):
        with self.assertRaises(ValidationError):
            self.env['hr.leave'].sudo().create({
                'employee_id': self.employee.id,
                'holiday_status_id': self.ext_type.id,
                'request_date_from': date(2027, 8, 1),
                'request_date_to': date(2027, 8, 5),
            })

    def test_cannot_extend_a_vacation_still_being_approved(self):
        parent = self._make_vacation(offset=1)
        self._advance_to(parent, 'pending_hr')
        with self.assertRaises(ValidationError):
            self._make_extension(parent)

    def test_cannot_extend_another_employees_vacation(self):
        parent = self._finalise(self._make_vacation(offset=2))
        other = self.env['hr.employee'].create({
            'name': 'Someone Else',
            'leave_manager_id': self.user_dm.id,
            'department_id': self.department.id,
        })
        with self.assertRaises(ValidationError):
            self._make_extension(parent, employee_id=other.id)

    def test_only_one_live_extension_per_vacation(self):
        parent = self._finalise(self._make_vacation(offset=3))
        self._make_extension(parent)
        with self.assertRaises(ValidationError):
            self._make_extension(parent)

    def test_extensions_chain_one_after_another(self):
        """An extension can itself be extended, contiguously."""
        parent = self._finalise(self._make_vacation(offset=4))
        first = self._finalise(self._make_extension(parent))
        second = self._make_extension(first)
        self.assertEqual(
            second.request_date_from,
            first.request_date_to + timedelta(days=1))
        self.assertEqual(second._extension_root(), parent)

    def test_a_plain_vacation_cannot_carry_the_link(self):
        parent = self._finalise(self._make_vacation(offset=5))
        with self.assertRaises(ValidationError):
            self._make_vacation(offset=6).sudo().write({
                'x_extended_leave_id': parent.id,
            })

    # ==================================================================
    # Only the current vacation may be extended
    # ==================================================================

    def test_only_the_current_vacation_is_offered(self):
        """The whole history would be a list of wrong answers."""
        old = self._finalise(self._make_vacation(offset=36))
        current = self._finalise(self._make_vacation(offset=37))
        Leave = self.env['hr.leave']
        self.assertEqual(Leave._extendable_vacation(self.employee), current)

    def test_the_picker_offers_exactly_one_vacation(self):
        parent = self._finalise(self._make_vacation(offset=38))
        ext = self._make_extension(parent)
        self.assertEqual(ext.x_extendable_leave_ids, parent)

    def test_extending_an_overtaken_vacation_is_refused(self):
        old = self._finalise(self._make_vacation(offset=39))
        self._finalise(self._make_vacation(offset=40))
        with self.assertRaises(ValidationError):
            self._make_extension(old)

    def test_the_picker_moves_on_to_the_extension(self):
        """Once an extension exists it is the current record, not its parent."""
        parent = self._finalise(self._make_vacation(offset=41))
        first = self._finalise(self._make_extension(parent))
        Leave = self.env['hr.leave']
        self.assertEqual(Leave._extendable_vacation(self.employee), first)

    def test_a_confirmed_return_closes_the_door(self):
        parent = self._finalise(self._make_vacation(offset=42))
        return_date = parent.request_date_to + timedelta(days=1)
        parent.sudo().write({'x_return_date': return_date})
        parent.with_user(self.user_dm).sudo().action_confirm_return_manager()
        Leave = self.env['hr.leave']
        self.assertFalse(Leave._extendable_vacation(self.employee))

    # ==================================================================
    # The Extend button on the vacation itself
    # ==================================================================

    def test_can_extend_only_on_the_current_vacation(self):
        old = self._finalise(self._make_vacation(offset=43))
        current = self._finalise(self._make_vacation(offset=44))
        self.assertFalse(old.with_user(self.user_hr).x_can_extend)
        self.assertTrue(current.with_user(self.user_hr).x_can_extend)

    def test_can_extend_is_false_for_an_unrelated_user(self):
        current = self._finalise(self._make_vacation(offset=45))
        self.assertFalse(current.with_user(self.user_acc).x_can_extend)
        self.assertTrue(current.with_user(self.user_dm).x_can_extend)

    def test_can_extend_is_false_while_still_being_approved(self):
        leave = self._make_vacation(offset=46)
        self._advance_to(leave, 'pending_acc')
        self.assertFalse(leave.with_user(self.user_hr).x_can_extend)

    def test_extend_button_opens_a_prefilled_extension(self):
        parent = self._finalise(self._make_vacation(offset=47))
        action = parent.with_user(self.user_hr).sudo().action_extend_vacation()
        ctx = action['context']
        self.assertEqual(ctx['default_x_extended_leave_id'], parent.id)
        self.assertEqual(ctx['default_employee_id'], self.employee.id)
        self.assertEqual(
            ctx['default_request_date_from'],
            parent.request_date_to + timedelta(days=1))
        self.assertTrue(
            self.env['hr.leave.type'].browse(
                ctx['default_holiday_status_id']).is_leave_extension)

    def test_extend_button_refuses_an_overtaken_vacation(self):
        """The point of the button: an old vacation cannot be extended."""
        old = self._finalise(self._make_vacation(offset=48))
        current = self._finalise(self._make_vacation(offset=49))
        with self.assertRaises(UserError):
            old.with_user(self.user_hr).sudo().action_extend_vacation()

    def test_extend_button_refuses_before_gm_final(self):
        leave = self._make_vacation(offset=50)
        self._advance_to(leave, 'pending_acc')
        with self.assertRaises(UserError):
            leave.with_user(self.user_hr).sudo().action_extend_vacation()

    def test_extend_button_refuses_an_unrelated_user(self):
        parent = self._finalise(self._make_vacation(offset=51))
        with self.assertRaises(UserError):
            parent.with_user(self.user_acc).action_extend_vacation()

    def test_extend_button_chains_from_the_extension(self):
        parent = self._finalise(self._make_vacation(offset=52))
        first = self._finalise(self._make_extension(parent))
        self.assertFalse(parent.with_user(self.user_hr).x_can_extend)
        action = first.with_user(self.user_hr).sudo().action_extend_vacation()
        self.assertEqual(action['context']['default_x_extended_leave_id'],
                         first.id)

    def test_the_vacation_is_preselected(self):
        """One candidate, so it is filled in rather than asked for."""
        parent = self._finalise(self._make_vacation(offset=53))
        form = self.env['hr.leave'].sudo().new({
            'employee_id': self.employee.id,
            'holiday_status_id': self.ext_type.id,
        })
        form._onchange_extension_link()
        self.assertEqual(form.x_extended_leave_id, parent)
        self.assertEqual(form.request_date_from,
                         parent.request_date_to + timedelta(days=1))

    # ==================================================================
    # The fees
    # ==================================================================

    def test_passport_fee_is_a_fixed_120(self):
        parent = self._finalise(self._make_vacation(offset=7))
        ext = self._make_extension(parent)
        self.assertEqual(ext.x_ext_passport_fee, 0.0)
        ext.sudo().write({'x_ext_passport_fee_applied': True})
        self.assertEqual(ext.x_ext_passport_fee, 120.0)
        ext.sudo().write({'x_ext_passport_fee_applied': False})
        self.assertEqual(ext.x_ext_passport_fee, 0.0)

    def test_passport_fee_cannot_be_typed(self):
        """Odoo accepts a write to a stored readonly compute and keeps it, so
        'locked on 120' needs a server-side guard, not a readonly attribute."""
        parent = self._finalise(self._make_vacation(offset=8))
        ext = self._make_extension(parent)
        ext.sudo().write({'x_ext_passport_fee_applied': True})
        with self.assertRaises(UserError):
            ext.with_user(self.user_hr).write({'x_ext_passport_fee': 500.0})
        self.assertEqual(ext.x_ext_passport_fee, 120.0)

    def test_total_cannot_be_typed(self):
        parent = self._finalise(self._make_vacation(offset=35))
        ext = self._make_extension(parent)
        with self.assertRaises(UserError):
            ext.with_user(self.user_hr).write({'x_ext_total_fees': 9999.0})
        self.assertEqual(ext.x_ext_total_fees, 0.0)

    def test_total_is_the_sum_of_every_fee(self):
        parent = self._finalise(self._make_vacation(offset=9))
        ext = self._make_extension(parent)
        ext.sudo().write({
            'x_ext_work_permit_fee': 100.0,
            'x_ext_iqama_fee': 650.0,
            'x_ext_bank_fee': 25.5,
            'x_ext_passport_fee_applied': True,
            'x_ext_violations_fee': 300.0,
            'x_ext_extra_fee': 40.0,
        })
        self.assertAlmostEqual(ext.x_ext_total_fees, 1235.5, places=2)

    def test_fees_are_not_posted_to_payroll(self):
        """They are recorded on the request and nowhere else."""
        parent = self._finalise(self._make_vacation(offset=10))
        ext = self._make_extension(parent)
        ext.sudo().write({'x_ext_iqama_fee': 650.0})
        self._finalise(ext)
        self.assertFalse(ext.x_vacation_payslip_ids)
        self.assertFalse(self.env['ksw.deduction'].sudo().search([
            ('employee_id', '=', self.employee.id),
        ]))

    # ==================================================================
    # Who may fill what, and when
    # ==================================================================

    def test_only_hr_can_fill_the_fees(self):
        parent = self._finalise(self._make_vacation(offset=11))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_hr')
        with self.assertRaises(UserError):
            ext.with_user(self.user_acc).write({'x_ext_iqama_fee': 650.0})

    def test_hr_cannot_fill_the_fees_at_the_wrong_step(self):
        parent = self._finalise(self._make_vacation(offset=12))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_acc')
        with self.assertRaises(UserError):
            ext.with_user(self.user_hr).write({'x_ext_iqama_fee': 650.0})

    def test_hr_fills_the_fees_at_its_own_step(self):
        parent = self._finalise(self._make_vacation(offset=13))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_hr')
        ext.with_user(self.user_hr).write({'x_ext_iqama_fee': 650.0})
        self.assertEqual(ext.x_ext_iqama_fee, 650.0)

    def test_clearing_a_fee_is_never_blocked(self):
        """The chain reset has to be able to blank them without being HR."""
        parent = self._finalise(self._make_vacation(offset=14))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_hr')
        ext.with_user(self.user_hr).write({'x_ext_iqama_fee': 650.0})
        self._advance_to(ext, 'pending_acc')
        ext.with_user(self.user_acc).write({'x_ext_iqama_fee': 0.0})
        self.assertEqual(ext.x_ext_iqama_fee, 0.0)

    def test_only_accounting_can_write_the_note(self):
        parent = self._finalise(self._make_vacation(offset=15))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_acc')
        with self.assertRaises(UserError):
            ext.with_user(self.user_hr).write({'x_ext_acc_note': 'Nope.'})
        ext.with_user(self.user_acc).write({'x_ext_acc_note': 'Checked.'})
        self.assertEqual(ext.x_ext_acc_note, 'Checked.')

    def test_accounting_cannot_write_the_note_at_the_wrong_step(self):
        parent = self._finalise(self._make_vacation(offset=16))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_hr')
        with self.assertRaises(UserError):
            ext.with_user(self.user_acc).write({'x_ext_acc_note': 'Too early.'})

    def test_a_gm_return_clears_the_fees(self):
        parent = self._finalise(self._make_vacation(offset=17))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_hr')
        ext.with_user(self.user_hr).write({'x_ext_iqama_fee': 650.0})
        ext.sudo()._reset_annual_multi_fields()
        self.assertEqual(ext.x_ext_iqama_fee, 0.0)
        self.assertFalse(ext.x_ext_passport_fee_applied)

    # ==================================================================
    # No vacation payslip
    # ==================================================================

    def test_extension_gets_no_vacation_payslip(self):
        parent = self._finalise(self._make_vacation(offset=18))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_employee_signature')
        self.assertFalse(ext.x_vacation_payslip_ids)

    def test_the_complement_still_reaches_the_payroll_implementation(self):
        """Filtering the extension out must not drop the vacation with it."""
        parent = self._finalise(self._make_vacation(offset=19))
        ext = self._make_extension(parent)
        seen = []

        def _spy(records, preview=False):
            seen.append(set(records.ids))
            return True

        with patch.object(PayrollHrLeave, '_create_vacation_payslip', _spy):
            (parent | ext).sudo()._create_vacation_payslip()
        self.assertEqual(seen, [{parent.id}])

    # ==================================================================
    # The return gate
    # ==================================================================

    def test_extension_takes_the_vacations_gate_over(self):
        parent = self._finalise(self._make_vacation(offset=20))
        self.assertEqual(parent.x_return_state, 'on_vacation')
        ext = self._finalise(self._make_extension(parent))
        self.assertEqual(ext.x_return_state, 'on_vacation')
        self.assertEqual(parent.x_return_state, 'extended')

    def test_the_gate_is_handed_over_only_once_the_extension_validates(self):
        """Between GM final and HR confirmation the vacation still blocks."""
        parent = self._finalise(self._make_vacation(offset=21))
        ext = self._make_extension(parent)
        self._advance_to(ext, 'pending_employee_signature')
        self.assertEqual(parent.x_return_state, 'on_vacation')

    def test_refusing_the_extension_gives_the_gate_back(self):
        parent = self._finalise(self._make_vacation(offset=22))
        ext = self._finalise(self._make_extension(parent))
        self.assertEqual(parent.x_return_state, 'extended')
        ext.sudo().action_refuse()
        self.assertEqual(parent.x_return_state, 'on_vacation')
        self.assertEqual(ext.x_return_state, 'not_applicable')

    def test_resetting_the_extension_to_draft_gives_the_gate_back(self):
        parent = self._finalise(self._make_vacation(offset=23))
        ext = self._finalise(self._make_extension(parent))
        ext.sudo().action_refuse()
        ext.sudo().action_draft()
        self.assertEqual(parent.x_return_state, 'on_vacation')
        self.assertEqual(ext.x_annual_approval_state, 'pending_dm')

    def test_deleting_the_extension_gives_the_gate_back(self):
        parent = self._finalise(self._make_vacation(offset=24))
        ext = self._finalise(self._make_extension(parent))
        self.assertEqual(parent.x_return_state, 'extended')
        ext.sudo().action_refuse()
        ext.sudo().unlink()
        self.assertEqual(parent.x_return_state, 'on_vacation')

    def test_payroll_waits_on_the_extension_not_the_vacation(self):
        parent = self._finalise(self._make_vacation(offset=25))
        ext = self._finalise(self._make_extension(parent))
        unresolved = self.env['hr.payslip'].sudo()._get_unresolved_vacation_leaves(
            self.employee.id, ext.request_date_to + timedelta(days=10))
        self.assertIn(ext, unresolved)
        self.assertNotIn(parent, unresolved)

    # ==================================================================
    # Confirming the return
    # ==================================================================

    def test_confirming_the_return_closes_the_whole_chain(self):
        parent = self._finalise(self._make_vacation(offset=26))
        ext = self._finalise(self._make_extension(parent))
        return_date = ext.request_date_to + timedelta(days=1)

        ext.sudo().write({'x_return_date': return_date})
        ext.with_user(self.user_dm).sudo().action_confirm_return_manager()

        self.assertEqual(ext.x_return_state, 'hr_confirmed')
        self.assertEqual(parent.x_return_state, 'hr_confirmed')
        self.assertEqual(parent.x_return_date, return_date)

    def test_confirming_the_return_restarts_the_annual_accrual(self):
        """The vacation underneath is never confirmed by anyone else, and it
        is that confirmation which moves the accrual baseline."""
        parent = self._finalise(self._make_vacation(offset=27))
        ext = self._finalise(self._make_extension(parent))
        return_date = ext.request_date_to + timedelta(days=1)

        ext.sudo().write({'x_return_date': return_date})
        ext.with_user(self.user_dm).sudo().action_confirm_return_manager()

        record = self.env['ksw.annual.leave'].sudo().search(
            [('employee_id', '=', self.employee.id)], limit=1)
        self.assertTrue(record, 'the employee has no balance record')
        self.assertEqual(record.x_opening_reset_date, return_date)

    def test_an_early_return_shortens_the_extension(self):
        """Unpaid days are deducted in arrears — a day trimmed is a day paid."""
        parent = self._finalise(self._make_vacation(offset=28))
        ext = self._finalise(self._make_extension(parent, days=9))
        planned_end = ext.request_date_to
        return_date = planned_end - timedelta(days=3)

        ext.sudo().write({'x_return_date': return_date})
        ext.with_user(self.user_dm).sudo().action_confirm_return_manager()

        self.assertEqual(ext.request_date_to, return_date - timedelta(days=1))
        self.assertLess(ext.request_date_to, planned_end)

    def test_a_chained_extension_closes_every_ancestor(self):
        parent = self._finalise(self._make_vacation(offset=29))
        first = self._finalise(self._make_extension(parent))
        second = self._finalise(self._make_extension(first))
        return_date = second.request_date_to + timedelta(days=1)

        second.sudo().write({'x_return_date': return_date})
        second.with_user(self.user_dm).sudo().action_confirm_return_manager()

        for leave in (parent, first, second):
            self.assertEqual(leave.x_return_state, 'hr_confirmed')
            self.assertEqual(leave.x_return_date, return_date)

    # ==================================================================
    # Unpaid semantics
    # ==================================================================

    def test_extension_days_are_not_covered(self):
        """Covered means paid; an extension explains the day, it does not
        pay for it (gotcha #44)."""
        parent = self._finalise(self._make_vacation(offset=30))
        ext = self._finalise(self._make_extension(parent))
        self.assertFalse(ext._excuses_absence())
        self.assertEqual(parent._excuses_absence(), parent)

    def test_duration_counts_calendar_days(self):
        parent = self._finalise(self._make_vacation(offset=31))
        ext = self._make_extension(parent, days=9)
        expected = (ext.request_date_to - ext.request_date_from).days + 1
        self.assertEqual(ext.number_of_days, expected)

    def test_an_unpaid_vacation_can_be_extended_too(self):
        parent = self._finalise(self._make_vacation(offset=32, unpaid=True))
        ext = self._finalise(self._make_extension(parent))
        self.assertEqual(parent.x_return_state, 'extended')
        self.assertEqual(ext.x_return_state, 'on_vacation')

    def test_an_extension_of_unpaid_leave_blocks_the_accrual_reset(self):
        """There is no annual vacation underneath it to settle."""
        parent = self._finalise(self._make_vacation(offset=33, unpaid=True))
        ext = self._finalise(self._make_extension(parent))
        self.assertIn(ext, ext._blocks_annual_reset())

    def test_an_extension_of_an_annual_vacation_does_not_block_it(self):
        parent = self._finalise(self._make_vacation(offset=34))
        ext = self._finalise(self._make_extension(parent))
        self.assertNotIn(ext, ext._blocks_annual_reset())
