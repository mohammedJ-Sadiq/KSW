"""*Who Gets Paid*, one employee at a time.

The General Manager reviews the month on the run's *Who Gets Paid* tab: one
line per employee, a figure, and a magnifier that opened every entry behind
it in one list — hours, days and meals under one quantity column, and back to
the tab before the next person.

This adds a review dialog on the register line: the entries split into a
section per component, each closed by its own subtotal, adding up to exactly
the *Earnings* on the line (each component rounded to whole riyals, the way
``ksw.pay.run._rounded_component_totals`` pays it). Rows that exist but are
not in this payment are listed apart with the reason. Previous / Next step
through the tab's own list without closing the dialog: each opens the next
line as a new ``target: 'new'`` action, which replaces the dialog in place.
"""
from odoo import _, api, fields, models


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    def action_review_employees(self):
        """Open the first employee of *Who Gets Paid*."""
        self.ensure_one()
        first = self.env['ksw.pay.run.line'].search(
            [('run_id', '=', self.id)], limit=1)
        if not first:
            return False
        return first.action_open_review()


class KswPayRunLine(models.Model):
    _inherit = 'ksw.pay.run.line'

    x_review_sections_html = fields.Html(
        compute='_compute_review_sections', sanitize=False,
        string='Breakdown')
    x_review_position = fields.Char(
        compute='_compute_review_position', string='Position')
    x_review_has_prev = fields.Boolean(
        compute='_compute_review_position', string='Has Previous')
    x_review_has_next = fields.Boolean(
        compute='_compute_review_position', string='Has Next')

    def _review_siblings(self):
        """The tab's own list: the lines this user may see, in its order."""
        self.ensure_one()
        return self.search([('run_id', '=', self.run_id.id)])

    @api.depends_context('uid')
    @api.depends('run_id', 'employee_id')
    def _compute_review_position(self):
        for rec in self:
            ids = rec._review_siblings().ids if rec.id else []
            pos = ids.index(rec.id) if rec.id in ids else -1
            rec.x_review_position = (
                '%s / %s' % (pos + 1, len(ids)) if pos >= 0 else False)
            rec.x_review_has_prev = pos > 0
            rec.x_review_has_next = 0 <= pos < len(ids) - 1

    @api.depends('run_id.state', 'employee_id', 'earnings')
    def _compute_review_sections(self):
        Review = self.env['ksw.pay.employee.review']
        for rec in self:
            run = rec.run_id
            if not (run and rec.employee_id):
                rec.x_review_sections_html = False
                continue
            # sudo: this is the justification of a figure the user can
            # already read on the line, and the line's earnings are built
            # from every department's rows, not only the ones his own entry
            # rules would show him. Same selection as _build_register.
            mine = run.sudo()._all_entries().filtered(
                lambda e: e.employee_id == rec.employee_id)
            payable = run.sudo()._payable_entries(
                settled_only=not rec.is_preview) & mine
            held = run.sudo()._held_entries(payable)
            settled = payable.filtered('x_vacation_payslip_id')
            paid = payable - held - settled
            totals = run._rounded_component_totals(paid).get(
                rec.employee_id.id, {})
            excluded = (
                [(e, _('On vacation, held')) for e in held]
                + [(e, _('Paid on a vacation payslip')) for e in settled]
                + [(e, _('Not handed over to the GM yet'))
                   for e in mine - payable])
            rec.x_review_sections_html = Review._render_entry_sections(
                paid, rec.currency_id or self.env.company.currency_id,
                paid=totals, excluded=excluded, expected=rec.earnings)

    def action_open_review(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': '%s — %s' % (self.employee_id.name,
                                 self.run_id.display_name),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'views': [(self.env.ref(
                'KSW_commissions.view_ksw_pay_run_line_review_form').id,
                'form')],
            'target': 'new',
            'context': {'ksw_review_nav': True,
                        'dialog_size': 'extra-large'},
        }

    def _review_step(self, step):
        self.ensure_one()
        ids = self._review_siblings().ids
        pos = ids.index(self.id) + step if self.id in ids else 0
        pos = max(0, min(pos, len(ids) - 1))
        return self.browse(ids[pos]).action_open_review()

    def action_review_next(self):
        return self._review_step(1)

    def action_review_prev(self):
        return self._review_step(-1)
