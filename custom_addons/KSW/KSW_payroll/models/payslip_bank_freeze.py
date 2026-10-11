"""A payslip sent to the bank in a text file is frozen until payroll says so.

This shop exports the bank file and pays before confirming the batch, so
once a payslip leaves in a bank text file its NET is a payment. Anything
that recomputes it afterwards (the Compute Sheet button, a loan disbursed
after the export, a commission recovery) would make the payslip disagree
with the bank: batch 250 (August 2026) and the September batch both did.

But updates do keep arriving, and some of them must be applied. So a
frozen payslip does not refuse them and does not lose them. Each one is
worked out in a dry run (what the payslip WOULD become), kept as a pending
change (`ksw.payslip.pending.change`), and the Payroll Managers are told.
A manager either applies it (the payslip is unfrozen and recomputed; the
next text export freezes it again) or dismisses it (it is left for next
month). Exporting again while changes are pending asks first.
"""
from markupsafe import Markup

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools import float_compare

TXT_MODES = ('all_txt', 'specific_txt')


class _DryRun(Exception):
    """Raised inside a savepoint to throw a preview recompute away."""


def _check_manager(env, what):
    if not env.su and not env.user.has_group(
            'om_hr_payroll.group_hr_payroll_manager'):
        raise UserError(_('Only a Payroll Manager may %(what)s.', what=what))


class HrPayslip(models.Model):
    _inherit = 'hr.payslip'

    x_bank_exported_date = fields.Datetime(
        string='Sent to Bank On', readonly=True, copy=False,
        help='Set when this payslip was first written to a bank text file. '
             'A draft payslip with this date is frozen: changes to it are '
             'kept as pending changes until a Payroll Manager applies them.')
    x_bank_exported_by = fields.Many2one(
        'res.users', string='Sent to Bank By', readonly=True, copy=False)
    x_bank_frozen = fields.Boolean(
        string='Frozen', compute='_compute_bank_frozen')
    x_pending_change_ids = fields.One2many(
        'ksw.payslip.pending.change', 'slip_id', string='Pending Changes')
    x_pending_net = fields.Float(
        string='NET After Pending Changes', compute='_compute_bank_frozen')
    x_has_pending_change = fields.Boolean(compute='_compute_bank_frozen')

    @api.depends('state', 'x_bank_exported_date',
                 'x_pending_change_ids.state', 'x_pending_change_ids.net_after')
    def _compute_bank_frozen(self):
        for slip in self:
            slip.x_bank_frozen = slip._ksw_is_bank_frozen()
            pending = slip.x_pending_change_ids.filtered(
                lambda p: p.state == 'pending')[:1]
            slip.x_has_pending_change = bool(pending)
            slip.x_pending_net = pending.net_after if pending else 0.0

    def _ksw_is_bank_frozen(self):
        self.ensure_one()
        return self.state == 'draft' and bool(self.x_bank_exported_date)

    # ------------------------------------------------------------------
    # Holding changes back
    # ------------------------------------------------------------------
    def compute_sheet(self):
        frozen = self.sudo().filtered(lambda s: s._ksw_is_bank_frozen())
        if not frozen:
            return super().compute_sheet()
        live = self - frozen
        res = super(HrPayslip, live).compute_sheet() if live else True
        queued = self.env['hr.payslip']
        for slip in self.browse(frozen.ids):
            if slip._ksw_hold_recompute():
                queued |= slip
        message = (
            _('%(n)s payslip(s) were already sent to the bank, so the change '
              'was not applied. It is waiting under Pending Changes on the '
              'batch for a Payroll Manager.', n=len(queued))
            if queued else
            _('These payslips were already sent to the bank. Recomputing '
              'them would change nothing, so nothing is pending.'))
        return {
            'type': 'ir.actions.client', 'tag': 'display_notification',
            'params': {'title': _('Payslip frozen'), 'message': message,
                       'type': 'warning', 'sticky': False},
        }

    def _ksw_line_totals(self):
        totals = {}
        for line in self.line_ids:
            totals[line.code] = totals.get(line.code, 0.0) + line.total
        return totals

    def _ksw_preview_recompute(self):
        """(before, after) line totals of a recompute that is thrown away."""
        self.ensure_one()
        before = self._ksw_line_totals()
        net_before = self._ksw_net_amount()
        self.env.flush_all()
        after, net_after = {}, net_before
        try:
            with self.env.cr.savepoint():
                super(HrPayslip, self).compute_sheet()
                self.env.flush_all()
                self.invalidate_recordset()
                after = self._ksw_line_totals()
                net_after = self._ksw_net_amount()
                raise _DryRun()
        except _DryRun:
            pass
        # A savepoint rollback does not clear the ORM cache.
        self.env.invalidate_all()
        return before, net_before, after, net_after

    def _ksw_hold_recompute(self):
        """Record what recomputing this frozen slip would change.

        Returns the pending change, or False when it would change nothing.
        """
        self.ensure_one()
        before, net_before, after, net_after = self._ksw_preview_recompute()
        diff = []
        for code in sorted(set(before) | set(after)):
            b, a = before.get(code, 0.0), after.get(code, 0.0)
            if float_compare(a, b, precision_digits=2):
                diff.append('%s: %s → %s' % (code, '{:,.2f}'.format(b),
                                            '{:,.2f}'.format(a)))
        if not diff:
            return False
        Pending = self.env['ksw.payslip.pending.change'].sudo()
        now = fields.Datetime.now()
        reason = self.env.context.get('ksw_pending_reason') or _(
            'Recompute requested')
        entry = '%s · %s · %s' % (
            fields.Datetime.to_string(now), self.env.user.name, reason)
        diff_text = '\n'.join(diff)
        rec = Pending.search([('slip_id', '=', self.id),
                              ('state', '=', 'pending')], limit=1)
        vals = {'net_current': net_before, 'net_after': net_after,
                'diff_text': diff_text, 'last_detected': now}
        if rec:
            news = (float_compare(rec.net_after, net_after, precision_digits=2)
                    or rec.diff_text != diff_text)
            vals['trigger_log'] = (rec.trigger_log or '') + '\n' + entry
            rec.write(vals)
        else:
            vals.update({'slip_id': self.id, 'first_detected': now,
                         'trigger_log': entry})
            rec = Pending.create(vals)
            news = True
        if news:
            rec._notify_managers()
        return rec

    def unlink(self):
        frozen = self.sudo().filtered(lambda s: s._ksw_is_bank_frozen())
        if frozen:
            raise UserError(_(
                'These payslips were already sent to the bank, so they '
                'cannot be deleted:\n%(rows)s',
                rows='\n'.join('• %s' % s.employee_id.name
                               for s in frozen[:20])))
        return super().unlink()

    def write(self, vals):
        entering_done = self.env['hr.payslip']
        if vals.get('state') == 'done':
            entering_done = self.filtered(lambda s: s.state != 'done')
        res = super().write(vals)
        if entering_done:
            # Confirmed as sent: what was still pending goes to next month
            # (or to a Payslip Revision), it can no longer apply here.
            pending = entering_done.sudo().x_pending_change_ids.filtered(
                lambda p: p.state == 'pending')
            if pending:
                pending._close('expired', _(
                    'Payslip confirmed as sent to the bank; the change was '
                    'not applied to it.'))
        return res


class KswPayslipPendingChange(models.Model):
    _name = 'ksw.payslip.pending.change'
    _description = 'Payslip Change Waiting Behind the Bank Freeze'
    _order = 'state, last_detected desc'

    slip_id = fields.Many2one('hr.payslip', required=True, ondelete='cascade',
                              index=True, readonly=True)
    run_id = fields.Many2one(related='slip_id.payslip_run_id', store=True,
                             index=True, string='Batch')
    employee_id = fields.Many2one(related='slip_id.employee_id', store=True,
                                  string='Employee')
    state = fields.Selection([
        ('pending', 'Pending'),
        ('applied', 'Applied'),
        ('dismissed', 'Dismissed'),
        ('expired', 'Not Applied (Confirmed)'),
    ], default='pending', required=True, readonly=True, index=True)
    net_current = fields.Float(string='NET Sent to Bank', readonly=True)
    net_after = fields.Float(string='NET If Applied', readonly=True)
    net_difference = fields.Float(compute='_compute_net_difference',
                                  string='Difference')
    diff_text = fields.Text(string='What Changes', readonly=True)
    trigger_log = fields.Text(string='Triggered By', readonly=True)
    first_detected = fields.Datetime(readonly=True)
    last_detected = fields.Datetime(string='Detected', readonly=True)
    closed_by = fields.Many2one('res.users', readonly=True)
    closed_date = fields.Datetime(readonly=True)
    net_applied = fields.Float(string='NET Applied', readonly=True)

    @api.depends('net_current', 'net_after')
    def _compute_net_difference(self):
        for rec in self:
            rec.net_difference = rec.net_after - rec.net_current

    def _payroll_manager_partners(self):
        group = self.env.ref('om_hr_payroll.group_hr_payroll_manager')
        return group.sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share).partner_id

    def _notify_managers(self):
        for rec in self:
            run = rec.run_id.sudo()
            body = Markup(
                '<strong>&#9203; %(title)s</strong><br/>'
                '%(emp)s: NET sent to bank %(cur)s, would become %(new)s.<br/>'
                '<pre>%(diff)s</pre>%(note)s'
            ) % {
                'title': _('Change waiting: payslip already sent to the bank'),
                'emp': rec.employee_id.name,
                'cur': '{:,.2f}'.format(rec.net_current),
                'new': '{:,.2f}'.format(rec.net_after),
                'diff': rec.diff_text,
                'note': _('Apply it under Pending Changes to unfreeze the '
                          'payslip, or dismiss it to leave it for next month.'),
            }
            target = run or rec.slip_id.sudo()
            target.message_post(
                body=body, subtype_xmlid='mail.mt_comment',
                partner_ids=rec._payroll_manager_partners().ids)

    def _close(self, state, note):
        self.sudo().write({'state': state, 'closed_by': self.env.uid,
                           'closed_date': fields.Datetime.now()})
        for run in self.run_id:
            mine = self.filtered(lambda p: p.run_id == run)
            run.sudo().message_post(body=Markup(
                '<strong>%(title)s</strong><br/>%(note)s<ul>%(rows)s</ul>'
            ) % {
                'title': dict(self._fields['state'].selection)[state],
                'note': note,
                'rows': Markup('').join(
                    Markup('<li>%(emp)s: %(a)s → %(b)s</li>') % {
                        'emp': p.employee_id.name,
                        'a': '{:,.2f}'.format(p.net_current),
                        'b': '{:,.2f}'.format(p.net_after)} for p in mine),
            }, subtype_xmlid='mail.mt_note')

    def action_apply(self):
        """Unfreeze the payslip and let the change in.

        The next bank text export freezes it again with the new figures.
        """
        _check_manager(self.env, _('apply a pending payslip change'))
        todo = self.filtered(lambda p: p.state == 'pending')
        for rec in todo:
            slip = rec.slip_id
            if slip.state != 'draft':
                raise UserError(_(
                    '%(emp)s: the payslip is no longer a draft, so the change '
                    'cannot be applied. Use a Payslip Revision.',
                    emp=rec.employee_id.name))
            slip.sudo().write({'x_bank_exported_date': False,
                               'x_bank_exported_by': False})
            # Authority was checked above; the recompute reads contract
            # fields (wage) a Payroll Manager need not hold rights to.
            slip.sudo().compute_sheet()
            rec.sudo().write({'net_applied': slip._ksw_net_amount()})
        if todo:
            todo._close('applied', _(
                'Payslip unfrozen and recomputed. Any bank file exported '
                'before this is out of date: export it again.'))
        return True

    def action_dismiss(self):
        _check_manager(self.env, _('dismiss a pending payslip change'))
        todo = self.filtered(lambda p: p.state == 'pending')
        if todo:
            todo._close('dismissed', _(
                'Left out of this payslip; the payslip stays as sent to '
                'the bank.'))
        return True


class HrPayslipRun(models.Model):
    _inherit = 'hr.payslip.run'

    x_frozen_slip_count = fields.Integer(
        string='Frozen Payslips', compute='_compute_freeze_counts')
    x_pending_change_ids = fields.One2many(
        'ksw.payslip.pending.change', 'run_id', string='Pending Changes')
    x_pending_change_count = fields.Integer(compute='_compute_freeze_counts')

    @api.depends('slip_ids.state', 'slip_ids.x_bank_exported_date',
                 'x_pending_change_ids.state')
    def _compute_freeze_counts(self):
        for run in self:
            run.x_frozen_slip_count = len(run.slip_ids.filtered(
                lambda s: s._ksw_is_bank_frozen()))
            run.x_pending_change_count = len(run.x_pending_change_ids.filtered(
                lambda p: p.state == 'pending'))

    def _ksw_pending_changes(self):
        return self.x_pending_change_ids.filtered(
            lambda p: p.state == 'pending')

    def action_apply_pending_changes(self):
        self.ensure_one()
        return self._ksw_pending_changes().action_apply()

    def _ksw_stamp_bank_export(self, slips):
        """Freeze ``slips`` (the draft ones not frozen yet) as sent now.

        The first date is kept: re-exporting the same figures changes
        nothing, and the first date is when they left.
        """
        self.ensure_one()
        new = slips.filtered(
            lambda s: s.state == 'draft' and not s.x_bank_exported_date)
        if not new:
            return
        # A system stamp after an authorised export; the value lock would
        # otherwise refuse it for a Payroll Officer.
        new.sudo().with_context(_ksw_skip_bank_refresh=True).write({
            'x_bank_exported_date': fields.Datetime.now(),
            'x_bank_exported_by': self.env.uid,
        })
        counts = {}
        for slip in new:
            bank = self._resolve_slip_bank_account(slip)
            label = bank.display_name or _('No bank account')
            counts[label] = counts.get(label, 0) + 1
        self.sudo().message_post(body=Markup(
            '<strong>&#128274; %(title)s</strong><br/>%(note)s<ul>%(rows)s</ul>'
        ) % {
            'title': _('%(n)s payslip(s) sent to the bank and frozen',
                       n=len(new)),
            'note': _('Changes to them now wait under Pending Changes until '
                      'a Payroll Manager applies them.'),
            'rows': Markup('').join(
                Markup('<li>%(bank)s: %(n)s</li>') % {'bank': b, 'n': n}
                for b, n in counts.items()),
        }, subtype_xmlid='mail.mt_note')

    def _ksw_superseded_slip_reasons(self, slips):
        """A frozen slip is never cancelled here: its money already left.

        It stays in the batch and is flagged instead, once, as a warning —
        the vacation / EOS payslip settles the same month, so this is a
        double payment to recover, not a slip to make disappear.
        """
        reasons = super()._ksw_superseded_slip_reasons(slips)
        frozen = [s for s in reasons if s._ksw_is_bank_frozen()]
        if not frozen:
            return reasons
        Skip = self.env['ksw.payslip.run.skip.line'].sudo()
        for slip in frozen:
            reason = reasons.pop(slip)
            run = slip.payslip_run_id
            note = _('%(reason)s Payslip %(slip)s was already sent to the '
                     'bank, so it was kept: this month is paid twice and '
                     'must be recovered.',
                     reason=reason,
                     slip=slip.number or slip.name or slip.id)
            if Skip.search_count([('run_id', '=', run.id),
                                  ('employee_id', '=', slip.employee_id.id),
                                  ('reason', '=', note)]):
                continue
            Skip.create({'run_id': run.id, 'employee_id': slip.employee_id.id,
                         'reason': note, 'line_type': 'warning'})
            run.sudo().message_post(
                body=Markup('<strong>&#9888; %(note)s</strong>') % {
                    'note': note},
                subtype_xmlid='mail.mt_note')
        return reasons
