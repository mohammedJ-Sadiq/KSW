"""A back-dated «الرد المضاعف» change re-weighs the Driver Trips it reaches.

Rule (user, 17 Nov 2026): approved and paid are different things.

* **Not paid** — draft, handed over, or approved by the GM but not yet
  marked paid: the entry's weighted quantity is corrected in place. An
  approved entry keeps its approval; its month's register line is
  re-derived (``ksw.pay.run._resync_vacation_line``), so the bank file and
  the BAS journal follow the new figure.
* **Paid** (``ksw.pay.run._months_paid_to``) or settled on a vacation
  payslip: nothing can be done to it. The difference in money goes to the
  driver's next open month as a «Driver Trips Remainder» line, which the GM
  approves with that month like any other.

Only the drivers BAS shows carrying this client's loads between the
effective date and the client's next change are touched; their quantity is
re-imported exactly as the importer computes it, so the result is the same
as importing the month again.
"""
from collections import defaultdict

from dateutil.relativedelta import relativedelta
from markupsafe import Markup

from odoo import _, fields, models

from odoo.addons.KSW_commissions.models.ksw_commission_lock import (
    HANDED_STATES, gm_for_department, period_is_locked,
)

REMAINDER = 'KSW_commissions.pay_component_gm_extra'
LOOKAHEAD_MONTHS = 12


class KswClientMultiplierRequestReweigh(models.Model):
    _inherit = 'ksw.client.multiplier.request'

    def _after_approve(self):
        res = super()._after_approve()
        for rec in self:
            rec._reweigh_driver_trips()
        return res

    def _reweigh_span(self):
        """Days whose loads now carry this request's value, both ends included."""
        self.ensure_one()
        start = max(self.effective_date,
                    self.env['ksw.pay.batch']._odoo_multiplier_from())
        until = self._rate_until()
        today = fields.Date.context_today(self)
        end = min(until, today) if until else today
        return start, end

    def _reweigh_driver_trips(self):
        self.ensure_one()
        Batch = self.env['ksw.pay.batch'].sudo()
        Run = self.env['ksw.pay.run'].sudo()
        start, end = self._reweigh_span()
        if not self.bas_code or start > end:
            return []
        lines = Batch._bas_customer_loads(
            start, end + relativedelta(days=1), bas_code=self.bas_code)
        drivers = {Batch._norm_cc(l['driver_cc']) for l in lines} - {''}
        if not drivers:
            self._post_reweigh([], start, end)
            return []

        components = self.env['ksw.pay.component'].sudo().search(
            [('importer', '=', 'bas_trips')])
        entries = self.env['ksw.pay.entry'].sudo().search([
            ('component_id', 'in', components.ids),
            ('employee_id', '!=', False),
            ('period', '>=', start.replace(day=1)),
            ('period', '<=', end),
        ], order='period, id')

        fetched = {}
        results = []
        touched = defaultdict(lambda: self.env['hr.employee'])
        for entry in entries:
            keys = Batch._bas_cost_centres(entry.employee_id)
            if not drivers & set(keys):
                continue
            month_start, month_end = entry._month_bounds()
            day_from = entry.x_window_from or month_start
            day_to = entry.x_window_to or month_end
            if day_to < start or day_from > end:
                continue
            if (day_from, day_to) not in fetched:
                fetched[(day_from, day_to)] = entry.batch_id._bas_fetch_orood(
                    day_from, day_to + relativedelta(days=1))
            data = Batch._bas_merge(fetched[(day_from, day_to)], keys)
            new_qty = round(data['mult'], 2) if data else 0.0
            old_qty = round(entry.quantity, 2)
            if abs(new_qty - old_qty) < 0.005:
                continue
            row = {'entry': entry, 'old': old_qty, 'new': new_qty}
            paid = entry.x_vacation_payslip_id or entry.period in \
                Run._months_paid_to(entry.employee_id, {entry.period})
            if new_qty <= 0:
                row['how'] = 'skipped'
                row['note'] = _('no weighted load left; left for a person to check')
            elif paid:
                row.update(self._carry_difference(entry, new_qty))
            else:
                old_amount = entry.amount
                entry.write({'quantity': new_qty})
                row.update(how='corrected', diff=entry.amount - old_amount)
                if entry.batch_id.run_id:
                    touched[entry.batch_id.run_id] |= entry.employee_id
            results.append(row)

        title = _('«الرد المضاعف» change %(ref)s', ref=self.name)
        for run, employees in touched.items():
            if run.state == 'approved':
                run._resync_vacation_line(employees, title=title)
            elif run.state not in ('approved', 'paid'):
                run._refresh_register()
        self._post_reweigh(results, start, end)
        return results

    def _carry_difference(self, entry, new_qty):
        """The paid month stays as paid; its difference goes to the next open one."""
        if entry.amount_override:
            return {'how': 'skipped',
                    'note': _('its amount was set by hand, so the difference '
                              'cannot be worked out')}
        _rate, new_amount = entry.component_id._resolve(
            entry.employee_id,
            quantity=new_qty,
            site=entry.site_id or entry.location_id,
            threshold=entry.threshold_qty,
            option=entry.option_id,
            date=entry.date or entry.period,
            band_scale=entry._band_scale(),
        )
        diff = round(new_amount - entry.amount, 2)
        if abs(diff) < 0.005:
            return {'how': 'paid_no_change', 'diff': 0.0}
        department = entry.batch_id.department_id
        target = self._next_open_batch(entry.employee_id, department, entry.period)
        if not target:
            return {'how': 'skipped', 'diff': diff,
                    'note': _('no open month found to carry the difference to')}
        carried = self.env['ksw.pay.entry'].sudo().create({
            'batch_id': target.id,
            'employee_id': entry.employee_id.id,
            'quantity': 1.0,
            'amount': diff,
            'reason': _('فرق ردود السائقين — %(ref)s: «الرد المضاعف» %(old)s → %(new)s '
                        'من %(day)s (%(month)s)',
                        ref=self.name, old=self.old_multiplier,
                        new=self.new_multiplier, day=self.effective_date,
                        month=entry.period.strftime('%m/%Y')),
            'details': _('%(month)s was already paid on %(qty_old).2f weighted trips; '
                         're-weighed it is %(qty_new).2f.',
                         month=entry.period.strftime('%B %Y'),
                         qty_old=entry.quantity, qty_new=new_qty),
        })
        return {'how': 'carried', 'diff': diff, 'carried': carried}

    def _next_open_batch(self, employee, department, paid_period):
        """The first month after ``paid_period`` that can still take a row for
        ``employee``: not paid to him, not locked, not handed over as a whole."""
        Batch = self.env['ksw.pay.batch'].sudo()
        Run = self.env['ksw.pay.run']
        component = self.env.ref(REMAINDER).sudo()
        gm = gm_for_department(self.env, department)
        period = paid_period + relativedelta(months=1)
        for _i in range(LOOKAHEAD_MONTHS):
            if not (Run._months_paid_to(employee, {period})
                    or period_is_locked(self.env, period, gm=gm)):
                batch = Batch.search([
                    ('component_id', '=', component.id),
                    ('department_id', '=', department.id),
                    ('period', '=', period), ('state', '=', 'draft'),
                ], limit=1)
                if batch and batch.submission_id.state not in HANDED_STATES + ('approved',):
                    return batch
                if not batch:
                    batch = Batch.create({
                        'component_id': component.id,
                        'department_id': department.id,
                        'period': period,
                    })
                    if batch.submission_id.state not in HANDED_STATES + ('approved',):
                        return batch
                    batch.unlink()
            period += relativedelta(months=1)
        return Batch

    def _post_reweigh(self, results, start, end):
        if not results:
            self.sudo().message_post(
                body=Markup(_('Driver trips: nothing to re-weigh between %(start)s and '
                              '%(end)s.')) % {'start': start, 'end': end},
                subtype_xmlid='mail.mt_note')
            return
        labels = {
            'corrected': _('corrected in place'),
            'carried': _('month paid: difference carried'),
            'paid_no_change': _('month paid: same amount'),
            'skipped': _('not changed'),
        }
        rows = Markup()
        for r in results:
            entry = r['entry']
            where = r.get('carried') and r['carried'].batch_id.display_name or ''
            rows += Markup(
                '<tr><td>%(emp)s</td><td>%(month)s</td><td>%(old).2f → %(new).2f</td>'
                '<td>%(diff)+.2f</td><td>%(how)s%(where)s%(note)s</td></tr>'
            ) % {
                'emp': entry.employee_id.name,
                'month': entry.period.strftime('%m/%Y'),
                'old': r['old'], 'new': r['new'], 'diff': r.get('diff') or 0.0,
                'how': labels[r['how']],
                'where': Markup(' (%s)') % where if where else '',
                'note': Markup(': %s') % r['note'] if r.get('note') else '',
            }
        body = Markup(
            '<b>%(title)s</b> %(start)s → %(end)s<br/>'
            '<table class="table table-sm"><tr><th>%(h_emp)s</th><th>%(h_month)s</th>'
            '<th>%(h_qty)s</th><th>%(h_diff)s</th><th>%(h_how)s</th></tr>%(rows)s</table>'
        ) % {
            'title': _('Driver trips re-weighed'), 'start': start, 'end': end,
            'h_emp': _('Driver'), 'h_month': _('Month'),
            'h_qty': _('Weighted trips'), 'h_diff': _('Amount'),
            'h_how': _('What happened'), 'rows': rows,
        }
        self.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
        for batch in {r['entry'].batch_id for r in results} | {
                r['carried'].batch_id for r in results if r.get('carried')}:
            batch.sudo().message_post(body=body, subtype_xmlid='mail.mt_note')
