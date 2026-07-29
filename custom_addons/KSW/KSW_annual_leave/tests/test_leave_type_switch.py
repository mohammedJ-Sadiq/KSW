"""Tests for re-syncing the multi-step chain when the leave type changes.

``x_annual_approval_state`` used to be stamped in ``create()`` only, so a
request created with a stock type (e.g. Sick) and then edited to an
Annual/EOS type kept the field empty forever — the KSW statusbar stayed
hidden, every approval button stayed hidden, and the record fell back to
showing the stock 2-step statusbar (real case: production leave 4891).
The reverse edit left a stale ``pending_*`` value, which hid the *stock*
statusbar instead.

``_resync_multi_step_chain`` (called from ``write()`` whenever
``holiday_status_id`` is in vals) reconciles the two.  It is idempotent, so
a record already broken by the old behaviour heals on its next save.

Covered here:
  - switching INTO a chain stamps pending_dm and notifies the DM
  - switching OUT of a chain clears the chain and restores can_approve
  - switching between two chain types mid-chain restarts at pending_dm
    and wipes the approval stamps
  - re-saving the same type does not re-notify (idempotency)
  - a record broken by the old code self-heals on save
  - final states (approved / validate) are never rewound
  - can_approve refreshes after a type change (stale @api.depends)
"""
import base64
from datetime import date, timedelta

from odoo.tests.common import TransactionCase


class TestLeaveTypeSwitch(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def _mkuser(name, login, extra_groups=()):
            group_ids = [cls.env.ref('base.group_user').id]
            group_ids.extend(extra_groups)
            return cls.env['res.users'].create({
                'name': name,
                'login': login,
                'email': f'{login}@typeswitch.test',
                'group_ids': [(6, 0, group_ids)],
            })

        cls.user_dm = _mkuser('Switch DM', 'switch_dm')
        cls.user_hr = _mkuser(
            'Switch HR', 'switch_hr',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_hr').id])
        cls.user_acc = _mkuser(
            'Switch Acc', 'switch_acc',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_acc').id])
        cls.user_gm = _mkuser(
            'Switch GM', 'switch_gm',
            [cls.env.ref('KSW_annual_leave.group_annual_leave_gm').id])

        cls.emp_dm = cls.env['hr.employee'].create(
            {'name': 'Switch DM Emp', 'user_id': cls.user_dm.id})
        cls.emp_hr = cls.env['hr.employee'].create(
            {'name': 'Switch HR Emp', 'user_id': cls.user_hr.id})
        cls.emp_acc = cls.env['hr.employee'].create(
            {'name': 'Switch Acc Emp', 'user_id': cls.user_acc.id})
        cls.emp_gm = cls.env['hr.employee'].create(
            {'name': 'Switch GM Emp', 'user_id': cls.user_gm.id})

        cls.employee = cls.env['hr.employee'].create({
            'name': 'Requesting Employee Switch',
            'user_id': _mkuser('Emp Switch', 'emp_switch').id,
            'leave_manager_id': cls.user_dm.id,
        })
        cls.user_emp = cls.employee.user_id

        # Stock 2-step type — no KSW chain. 'both' matches what the
        # KSW_leave_approval post-init hook sets on every non-annual type.
        cls.type_stock = cls.env['hr.leave.type'].create({
            'name': 'Sick Leave Switch Test',
            'requires_allocation': False,
            'leave_validation_type': 'both',
        })
        cls.type_annual = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Switch Test',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })
        # A second annual_multi type: this is the EOS-shaped case — same
        # validation type, different type record — so the re-sync must key
        # off the type *id*, not the validation type.
        cls.type_annual_alt = cls.env['hr.leave.type'].create({
            'name': 'Annual Leave Switch Test (Alt)',
            'requires_allocation': False,
            'leave_validation_type': 'annual_multi',
            'is_annual_leave': True,
        })

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_leave(self, leave_type, offset=0):
        start = date(2029, 1, 1) + timedelta(days=offset * 15)
        return self.env['hr.leave'].sudo().create({
            'employee_id': self.employee.id,
            'holiday_status_id': leave_type.id,
            'request_date_from': start,
            'request_date_to': start + timedelta(days=4),
        })

    _STATE_RANK = {
        'pending_dm': 0, 'pending_hr': 1, 'pending_gm_initial': 2,
        'pending_acc': 3, 'pending_gm_final': 4,
        'pending_employee_signature': 5, 'approved': 6,
    }

    def _advance_to(self, leave, target_state):
        """Advance the approval chain to target_state (resume-aware)."""
        steps = [
            ('pending_dm', 'action_dm_approve', self.user_dm),
            ('pending_hr', 'action_hr_approve', self.user_hr),
            ('pending_gm_initial', 'action_gm_initial_approve', self.user_gm),
            ('pending_acc', 'action_acc_approve', self.user_acc),
            ('pending_gm_final', 'action_gm_final_approve', self.user_gm),
        ]
        for pre_state, method, user in steps:
            rank = self._STATE_RANK.get(leave.x_annual_approval_state, 0)
            if rank > self._STATE_RANK[pre_state]:
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

    # ==================================================================
    # Switching INTO the chain
    # ==================================================================

    def test_switch_into_chain_stamps_pending_dm(self):
        """Sick -> Annual starts the 6-step chain at pending_dm."""
        leave = self._make_leave(self.type_stock)
        self.assertFalse(leave.x_annual_approval_state)

        leave.sudo().write({'holiday_status_id': self.type_annual.id})

        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

    def test_switch_into_chain_notifies_dm(self):
        """The DM gets an inbox notification plus a 'type changed' note."""
        leave = self._make_leave(self.type_stock, offset=1)
        existing_ids = leave.message_ids.ids

        leave.sudo().write({'holiday_status_id': self.type_annual.id})

        new_msgs = leave.message_ids.filtered(
            lambda m: m.id not in existing_ids)
        dm_partner = self.user_dm.partner_id
        self.assertTrue(
            new_msgs.filtered(lambda m: dm_partner in m.partner_ids),
            'The direct manager should be notified when the request enters '
            'the multi-step chain.')
        self.assertTrue(
            new_msgs.filtered(lambda m: 'Leave Type Changed' in (m.body or '')),
            'A chatter note explaining the chain change should be posted.')

    def test_switch_into_chain_gives_dm_the_approve_button(self):
        """x_can_dm_approve becomes True for the DM after the switch."""
        leave = self._make_leave(self.type_stock, offset=2)
        leave.sudo().write({'holiday_status_id': self.type_annual.id})

        self.assertTrue(leave.with_user(self.user_dm).x_can_dm_approve)

    # ==================================================================
    # Switching OUT of the chain
    # ==================================================================

    def test_switch_out_of_chain_clears_state(self):
        """Annual -> Sick drops the chain so the stock flow takes over."""
        leave = self._make_leave(self.type_annual, offset=3)
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

        leave.sudo().write({'holiday_status_id': self.type_stock.id})

        self.assertFalse(leave.x_annual_approval_state)
        # The stock Approve button comes back: _compute_can_approve forces it
        # False for annual_multi leaves and defers to base otherwise. Asserted
        # as superuser so the result reflects the override alone and not the
        # acting user's record rules.
        self.assertTrue(leave.sudo().can_approve)

    def test_switch_out_of_chain_wipes_stamps(self):
        """Approval stamps from the abandoned chain are cleared."""
        leave = self._make_leave(self.type_annual, offset=4)
        self._advance_to(leave, 'pending_hr')
        self.assertTrue(leave.x_dm_approved_by)

        leave.sudo().write({'holiday_status_id': self.type_stock.id})

        self.assertFalse(leave.x_annual_approval_state)
        self.assertFalse(leave.x_dm_approved_by)
        self.assertFalse(leave.x_dm_approved_date)

    # ==================================================================
    # Switching BETWEEN two chain types
    # ==================================================================

    def test_mid_chain_type_switch_restarts_at_dm(self):
        """Approvals given for the previous type are discarded."""
        leave = self._make_leave(self.type_annual, offset=5)
        self._advance_to(leave, 'pending_gm_initial')
        self.assertEqual(leave.x_annual_approval_state, 'pending_gm_initial')

        leave.sudo().write({'holiday_status_id': self.type_annual_alt.id})

        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        self.assertFalse(leave.x_dm_approved_by)
        self.assertFalse(leave.x_dm_approved_date)
        self.assertFalse(leave.x_hr_approved_by)
        self.assertFalse(leave.x_hr_approved_date)

    def test_type_switch_at_pending_dm_stays_quiet(self):
        """Switching type before any approval keeps step 1 without re-notifying."""
        leave = self._make_leave(self.type_annual, offset=6)
        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        existing_ids = leave.message_ids.ids

        leave.sudo().write({'holiday_status_id': self.type_annual_alt.id})

        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        new_msgs = leave.message_ids.filtered(
            lambda m: m.id not in existing_ids)
        dm_partner = self.user_dm.partner_id
        self.assertFalse(
            new_msgs.filtered(lambda m: dm_partner in m.partner_ids),
            'The DM was already notified at creation and is still the '
            'current approver — no second notification is warranted.')

    # ==================================================================
    # Idempotency / self-healing
    # ==================================================================

    def test_resync_is_idempotent(self):
        """Re-writing the same type changes nothing and does not re-notify."""
        leave = self._make_leave(self.type_stock, offset=7)
        leave.sudo().write({'holiday_status_id': self.type_annual.id})
        message_count = len(leave.message_ids)

        leave.sudo().write({'holiday_status_id': self.type_annual.id})

        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')
        self.assertEqual(len(leave.message_ids), message_count)

    def test_self_heals_record_broken_by_old_code(self):
        """A leave left with an empty chain state heals on the next save.

        This reproduces production leave 4891: an annual_multi type whose
        x_annual_approval_state was never stamped.
        """
        leave = self._make_leave(self.type_annual, offset=8)
        leave.sudo().write({'x_annual_approval_state': False})
        self.assertFalse(leave.x_annual_approval_state)

        # Same type, re-saved — the healing branch does not need a change.
        leave.sudo().write({'holiday_status_id': self.type_annual.id})

        self.assertEqual(leave.x_annual_approval_state, 'pending_dm')

    # ==================================================================
    # Final states are never rewound
    # ==================================================================

    def test_final_states_are_not_rewound(self):
        """An approved leave keeps its chain state even if the type is written."""
        leave = self._make_leave(self.type_annual, offset=9)
        self._advance_to(leave, 'pending_employee_signature')
        self._attach(leave)
        leave.with_user(self.user_hr).sudo().action_employee_confirm_signature()
        self.assertEqual(leave.x_annual_approval_state, 'approved')
        self.assertEqual(leave.state, 'validate')

        leave.sudo().write({'holiday_status_id': self.type_stock.id})

        self.assertEqual(leave.x_annual_approval_state, 'approved')

    # ==================================================================
    # Stale @api.depends regression
    # ==================================================================

    def test_can_approve_refreshes_after_type_change(self):
        """can_approve must recompute when holiday_status_id changes."""
        leave = self._make_leave(self.type_stock, offset=10).sudo()
        self.assertTrue(leave.can_approve)

        leave.write({'holiday_status_id': self.type_annual.id})

        # No manual invalidate_cache: the @api.depends must cover it.
        self.assertFalse(leave.can_approve)
