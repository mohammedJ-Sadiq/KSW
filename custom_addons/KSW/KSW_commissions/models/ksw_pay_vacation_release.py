"""Releasing a month the vacation hold is holding.

The hold (see ``ksw_vacation_hold``) says: this employee went on vacation,
so the commission months he was owed were settled on the leave and the pay
run must not pay them again.  That is company policy, and policy has
exceptions — Accounting may simply not have listed the month, in which case
nobody has paid it and holding it would cost the employee real money.

So it is an **acceptance, not a wall**, the same shape as the concurrent-loan
warning in KSW_deduction: the block stands until somebody with the authority
puts his name and his reason against it, for one employee and one month.
"""
from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .ksw_commission_lock import normalise_period
from .ksw_vacation_hold import vacation_hold


class KswPayVacationRelease(models.Model):
    _name = 'ksw.pay.vacation.release'
    _description = 'KSW Vacation Hold Release'
    _order = 'period desc, id desc'

    employee_id = fields.Many2one(
        'hr.employee', required=True, ondelete='cascade', index=True,
        string='Employee',
    )
    period = fields.Date(
        required=True, index=True, string='Month',
        default=lambda s: fields.Date.context_today(s).replace(day=1),
        help='The month being released. Always stored as its first day.',
    )
    reason = fields.Text(
        required=True,
        help='Why this month is payable although the employee went on '
             'vacation — typically that Accounting did not list it on the '
             'leave, so it was never settled.',
    )
    # Recorded at creation rather than read live off the leave: the hold is
    # a moving answer (a later return confirmation changes it), and what
    # belongs on this record is what was true when the decision was taken.
    held_reason = fields.Char(
        readonly=True, string='Was Held Because',
        help='What the system was holding when this release was recorded.',
    )
    leave_id = fields.Many2one(
        'hr.leave', readonly=True, string='Vacation', ondelete='set null',
    )
    released_by = fields.Many2one(
        'res.users', readonly=True, string='Released By',
        default=lambda s: s.env.user,
    )
    released_date = fields.Datetime(
        readonly=True, string='Released On', default=fields.Datetime.now,
    )

    _unique_employee_period = models.Constraint(
        'UNIQUE(employee_id, period)',
        'That employee has already been released for that month.',
    )

    @api.depends('employee_id', 'period')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = '%s — %s' % (
                rec.employee_id.sudo().display_name or '',
                rec.period.strftime('%B %Y') if rec.period else '',
            )

    # ------------------------------------------------------------------
    # Authorisation
    # ------------------------------------------------------------------
    def _check_may_release(self):
        """Only this employee's General Manager, or the Administrator.

        A group answers "may you act as a GM at all", never "for whom" —
        so the department carries the answer, exactly as
        ``ksw.pay.submission._check_is_my_department`` does one screen over.
        """
        if self.env.su:
            return
        user = self.env.user
        if user.has_group('KSW_commissions.group_commission_officer'):
            return
        if not user.has_group('KSW_commissions.group_commission_gm'):
            raise UserError(_(
                "Only the General Manager can release a month that was "
                "settled on a vacation request."))
        default_gm = self.env.company.sudo().x_default_gm_id
        for rec in self:
            employee = rec.employee_id.sudo()
            gm = employee.department_id.x_effective_gm_id or default_gm
            if gm.sudo().user_id == user:
                continue
            raise UserError(_(
                "%(employee)s is not in a department you are the General "
                "Manager of, so releasing his month is not yours to do.",
                employee=employee.display_name,
            ))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('period'):
                vals['period'] = normalise_period(vals['period'])
        releases = super().create(vals_list)
        releases._check_may_release()
        releases._stamp_held_reason()
        releases._announce()
        return releases

    def write(self, vals):
        if vals.get('period'):
            vals['period'] = normalise_period(vals['period'])
        self._check_may_release()
        res = super().write(vals)
        self._check_may_release()
        return res

    def unlink(self):
        self._check_may_release()
        return super().unlink()

    def _stamp_held_reason(self):
        """Record what was being held, before the release makes it vanish.

        ``vacation_hold`` consults this very table, so it has to be asked
        with the new row pretended away — otherwise it answers "nothing is
        held", which is true only because of the row being created.
        """
        for rec in self:
            hold = vacation_hold(
                rec.with_context(ksw_ignore_release_ids=rec.ids).env,
                rec.employee_id, rec.period)
            if not hold:
                continue
            rec.leave_id = hold.leave.id
            rec.held_reason = hold.leave.holiday_status_id.sudo().display_name

    def _announce(self):
        """Put the decision on the vacation's own chatter.

        The leave is where anybody asking "was this month paid twice?" will
        look, and it is the document Accounting settles on.
        """
        for rec in self.filtered('leave_id'):
            rec.leave_id.sudo().message_post(
                body=Markup(
                    '<strong>💸 Commission month released</strong><br/>'
                    '<b>Month:</b> %(month)s<br/>'
                    '<b>By:</b> %(user)s<br/>'
                    '<b>Reason:</b> %(reason)s<br/>'
                    'The monthly commission run will pay this month, on the '
                    'understanding that it was not settled here.'
                ) % {
                    'month': rec.period.strftime('%B %Y'),
                    'user': rec.released_by.name or self.env.user.name,
                    'reason': rec.reason or '—',
                },
                subtype_xmlid='mail.mt_note',
            )
