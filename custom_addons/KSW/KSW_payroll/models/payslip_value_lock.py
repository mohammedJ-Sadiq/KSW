"""Payroll Officers review payroll; only the system and Managers change it.

Audit 2026-09-28: a Payroll Officer could edit the NET line and the inputs of
a *paid* payslip, recompute it, and raw-write it back to draft (the Manager-
only reversal lock guarded the buttons, not write()).

The rule now:

* **Officer** (``group_hr_payroll_user`` without Manager): creates a batch,
  generates its payslips, lets the system compute them, and reads
  everything. Never sets a value by hand, never changes a payslip's state,
  never deletes a payslip, never confirms (Mark as Done) — that is the
  Manager's.
* **Manager**: may correct a *draft* payslip and confirm.
* **A paid (``done``) payslip is sealed for everyone.** Its values and lines
  never change again; a correction is a Payslip Revision. The Manager's
  reversal buttons (cancel / set to draft / refund) remain the only way to
  move its state.

What the system writes while building or computing a payslip runs under the
``ksw_payslip_system`` context flag (or ``sudo()``), so generation and the
compute keep working for an Officer.
"""
from odoo import _, api, models
from odoo.exceptions import UserError

from .hr_payslip import PAYROLL_MANAGER_GROUP

SYSTEM_CTX = 'ksw_payslip_system'
_FREE_PREFIXES = ('message_', 'activity_', 'website_message_')
# Delivery bookkeeping, not payroll values: re-sending a payslip email is an
# Officer task and must work on a paid payslip too.
_FREE_FIELDS = frozenset({'x_email_state', 'x_email_error'})
_VALUE_O2M = ('line_ids', 'input_line_ids', 'worked_days_line_ids')


def _unrestricted(env):
    return env.su or env.context.get(SYSTEM_CTX)


def _is_manager(env):
    return env.user.has_group(PAYROLL_MANAGER_GROUP)


def _is_officer_only(env):
    """The tier this lock narrows. Anyone below it has no write access to
    payroll at all and gets Odoo's own AccessError from the ACL."""
    return (env.user.has_group('om_hr_payroll.group_hr_payroll_user')
            and not _is_manager(env))


def _free(fname):
    return fname.startswith(_FREE_PREFIXES) or fname in _FREE_FIELDS


def _officer_error():
    return UserError(_(
        'Payroll Officers can create a batch, generate its payslips and '
        'review them, but cannot change any payslip value or status. '
        'Please ask a Payroll Manager.'))


def _sealed_error():
    return UserError(_(
        'This payslip has already been paid, so its figures can no longer '
        'change. Issue a Payslip Revision to correct a paid period.'))


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    @api.model_create_multi
    def create(self, vals_list):
        if not _unrestricted(self.env) and _is_officer_only(self.env):
            # An Officer outside the generate wizard: never trust hand-made
            # figures. compute_sheet() rebuilds them from the real sources.
            for vals in vals_list:
                for key in _VALUE_O2M:
                    vals.pop(key, None)
        records = super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                        ).create(vals_list)
        return records.with_env(self.env)

    def write(self, vals):
        if not _unrestricted(self.env):
            keys = [k for k in vals if not _free(k)]
            if keys:
                # Access rights first: a user with no write access at all
                # (e.g. the read-only Payroll Reviewer) gets Odoo's own
                # AccessError, not this lock's explanation.
                self.check_access('write')
                if _is_officer_only(self.env):
                    raise _officer_error()
                if any(s.state == 'done' for s in self.sudo()):
                    raise _sealed_error()
        return super().write(vals)

    def unlink(self):
        if not _unrestricted(self.env) and _is_officer_only(self.env):
            self.check_access('unlink')
            raise _officer_error()
        return super().unlink()

    def compute_sheet(self):
        if not self.env.su and any(s.state == 'done' for s in self.sudo()):
            raise _sealed_error()
        if not self.env.su and not self.env.user.has_group(
                'om_hr_payroll.group_hr_payroll_user'):
            # No payroll tier at all: Odoo's own AccessError, not ours.
            self.check_access('write')
            raise _officer_error()
        return super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                     ).compute_sheet()

    def action_payslip_done(self):
        if not self.env.su and _is_officer_only(self.env):
            self.check_access('write')
            raise UserError(_(
                'Only a Payroll Manager may confirm payslips. Payroll '
                'Officers prepare and review them.'))
        return super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                     ).action_payslip_done()

    # The reversal buttons already refuse a non-Manager (_check_payroll_
    # manager); once past that check they are the sanctioned way to move a
    # paid payslip's state, so they run as a system operation.
    def action_payslip_cancel(self):
        self._check_payroll_manager(_('reject (cancel) a payslip'))
        return super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                     ).action_payslip_cancel()

    def action_payslip_draft(self):
        self._check_payroll_manager(_('reset a payslip to draft'))
        return super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                     ).action_payslip_draft()

    def refund_sheet(self):
        self._check_payroll_manager(_('refund a payslip'))
        return super(HrPayslip, self.with_context(**{SYSTEM_CTX: True})
                     ).refund_sheet()


def _check_child_value_change(records, payslip_field):
    """Lines, inputs and worked days: only the system and a Manager (on a
    draft payslip) may create, change or delete them."""
    if _unrestricted(records.env):
        return
    records.check_access('write')
    if _is_officer_only(records.env):
        raise _officer_error()
    if any(r[payslip_field].state == 'done' for r in records.sudo()):
        raise _sealed_error()


class HrPayslipLineLock(models.Model):
    _inherit = 'hr.payslip.line'

    @api.model_create_multi
    def create(self, vals_list):
        if not _unrestricted(self.env) and _is_officer_only(self.env):
            self.browse().check_access('create')
            raise _officer_error()
        records = super().create(vals_list)
        _check_child_value_change(records, 'slip_id')
        return records

    def write(self, vals):
        _check_child_value_change(self, 'slip_id')
        return super().write(vals)

    def unlink(self):
        _check_child_value_change(self, 'slip_id')
        return super().unlink()


class HrPayslipInputLock(models.Model):
    _inherit = 'hr.payslip.input'

    @api.model_create_multi
    def create(self, vals_list):
        if not _unrestricted(self.env) and _is_officer_only(self.env):
            self.browse().check_access('create')
            raise _officer_error()
        records = super().create(vals_list)
        _check_child_value_change(records, 'payslip_id')
        return records

    def write(self, vals):
        _check_child_value_change(self, 'payslip_id')
        return super().write(vals)

    def unlink(self):
        _check_child_value_change(self, 'payslip_id')
        return super().unlink()


class HrPayslipWorkedDaysLock(models.Model):
    _inherit = 'hr.payslip.worked_days'

    @api.model_create_multi
    def create(self, vals_list):
        if not _unrestricted(self.env) and _is_officer_only(self.env):
            self.browse().check_access('create')
            raise _officer_error()
        records = super().create(vals_list)
        _check_child_value_change(records, 'payslip_id')
        return records

    def write(self, vals):
        _check_child_value_change(self, 'payslip_id')
        return super().write(vals)

    def unlink(self):
        _check_child_value_change(self, 'payslip_id')
        return super().unlink()


class HrPayslipRun(models.Model):
    _inherit = 'hr.payslip.run'

    def write(self, vals):
        # An Officer creates and prepares a batch; once it is closed it is
        # the Manager's.
        if (not _unrestricted(self.env) and _is_officer_only(self.env)
                and any(r.state != 'draft' for r in self.sudo())
                and any(not _free(k) for k in vals)):
            self.check_access('write')
            raise _officer_error()
        return super().write(vals)

    def done_payslip_run(self):
        if not self.env.su and _is_officer_only(self.env):
            self.check_access('write')
            raise UserError(_(
                'Only a Payroll Manager may confirm a payslip batch (Mark as '
                'Done). Payroll Officers prepare and review it.'))
        return super(HrPayslipRun, self.with_context(**{SYSTEM_CTX: True})
                     ).done_payslip_run()


class HrPayslipEmployees(models.TransientModel):
    _inherit = 'hr.payslip.employees'

    def compute_sheet(self):
        # The generate wizard is the Officer's sanctioned way in: the
        # payslips it builds (and their figures) come from the system.
        return super(HrPayslipEmployees, self.with_context(**{SYSTEM_CTX: True})
                     ).compute_sheet()
