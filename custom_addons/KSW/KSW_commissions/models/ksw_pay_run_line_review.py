"""*Who Gets Paid*, one employee at a time.

The General Manager reviews the month on the run's *Who Gets Paid* tab: one
line per employee, a figure, and a magnifier that opened every entry behind
it in one list — hours, days and meals under one quantity column, and back to
the tab before the next person.

The review is a small session (``ksw.pay.run.review``) shown as a dialog:
the current employee's entries split into a section per component, each
closed by its own subtotal, adding up to exactly the *Earnings* on the line
(each component rounded to whole riyals, the way
``ksw.pay.run._rounded_component_totals`` pays it). Rows that exist but are
not in this payment are listed apart with the reason.

At the top of the dialog the reviewer sorts (by department, by name, by
amount) and narrows (departments, a name search) the list Previous / Next
walk through, without filtering the tab itself. The sort he last used is
remembered for him (``ir.default``), since most reviews go department by
department.
"""
from odoo import _, api, fields, models


def _name_key(line):
    # Names here carry leading spaces and mixed case (see _export_sorted).
    return (line.employee_id.sudo().name or '').strip().casefold()


SORT_KEYS = {
    'department': lambda l: (
        (l.department_id.sudo().name or '').strip().casefold(), _name_key(l)),
    'name': lambda l: (_name_key(l),),
    'earnings_desc': lambda l: (-(l.earnings or 0.0), _name_key(l)),
    'net_desc': lambda l: (-(l.net_payable or 0.0), _name_key(l)),
}


class KswPayRun(models.Model):
    _inherit = 'ksw.pay.run'

    def action_review_employees(self):
        """Open the review on the first employee of the user's order."""
        self.ensure_one()
        return self.env['ksw.pay.run.review']._open_for(self)


class KswPayRunLine(models.Model):
    _inherit = 'ksw.pay.run.line'

    x_review_sections_html = fields.Html(
        compute='_compute_review_sections', sanitize=False,
        string='Breakdown')

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
        """Open the review on this employee, in the user's usual order."""
        self.ensure_one()
        return self.env['ksw.pay.run.review']._open_for(self.run_id, self)


class KswPayRunReview(models.TransientModel):
    """One reviewer walking one month's register."""
    _name = 'ksw.pay.run.review'
    _description = 'KSW Pay Run Review'

    run_id = fields.Many2one('ksw.pay.run', required=True, readonly=True)
    line_id = fields.Many2one('ksw.pay.run.line', string='Employee Line')
    sort_by = fields.Selection([
        ('department', 'Department, then name'),
        ('name', 'Employee name'),
        ('earnings_desc', 'Earnings, highest first'),
        ('net_desc', 'Net payable, highest first'),
    ], string='Sort by', required=True, default='department')
    department_ids = fields.Many2many(
        'hr.department', string='Departments',
        help='Only these departments. Leave empty for everyone.')
    allowed_department_ids = fields.Many2many(
        'hr.department', compute='_compute_allowed_departments')
    search_text = fields.Char(string='Find')

    position = fields.Char(compute='_compute_position')
    has_prev = fields.Boolean(compute='_compute_position')
    has_next = fields.Boolean(compute='_compute_position')

    employee_id = fields.Many2one(related='line_id.employee_id')
    department_id = fields.Many2one(related='line_id.department_id')
    earnings = fields.Monetary(related='line_id.earnings')
    loan_offset = fields.Monetary(related='line_id.loan_offset')
    net_payable = fields.Monetary(related='line_id.net_payable')
    bank_account_id = fields.Many2one(related='line_id.bank_account_id')
    currency_id = fields.Many2one(related='run_id.currency_id')
    sections_html = fields.Html(
        related='line_id.x_review_sections_html', sanitize=False)

    # ------------------------------------------------------------------
    @api.model
    def _open_for(self, run, line=None):
        review = self.create({'run_id': run.id})
        ordered = review._ordered_lines()
        review.line_id = line if line and line in ordered else ordered[:1]
        return review._action()

    def _action(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Who Gets Paid — %(run)s',
                      run=self.run_id.display_name),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
            'context': {'dialog_size': 'extra-large'},
        }

    def _all_lines(self):
        """The tab's own list: the lines this user may see."""
        self.ensure_one()
        return self.env['ksw.pay.run.line'].search(
            [('run_id', '=', self.run_id.id)])

    def _ordered_lines(self):
        self.ensure_one()
        lines = self._all_lines()
        # _origin: inside an onchange the tags are new records (NewId), and
        # a real department is never "in" them, which emptied the list.
        departments = self.department_ids._origin
        if departments:
            lines = lines.filtered(lambda l: l.department_id in departments)
        if self.search_text and self.search_text.strip():
            needle = self.search_text.strip().casefold()
            lines = lines.filtered(lambda l: needle in _name_key(l))
        return lines.sorted(SORT_KEYS[self.sort_by or 'department'])

    @api.depends('run_id')
    def _compute_allowed_departments(self):
        for rec in self:
            rec.allowed_department_ids = (
                rec._all_lines().department_id if rec.run_id else False)

    @api.depends('line_id', 'sort_by', 'department_ids', 'search_text')
    def _compute_position(self):
        for rec in self:
            ids = rec._ordered_lines().ids if rec.run_id else []
            line_id = rec.line_id._origin.id or rec.line_id.id
            pos = ids.index(line_id) if line_id in ids else -1
            rec.position = '%s / %s' % (pos + 1 if pos >= 0 else 0, len(ids))
            rec.has_prev = pos > 0
            rec.has_next = 0 <= pos < len(ids) - 1

    @api.onchange('sort_by', 'department_ids', 'search_text')
    def _onchange_order(self):
        """Start the new list from its first employee. Keeping the one on
        screen when he was still in the list made a new sort look like it
        had done nothing: only the counter moved."""
        self.line_id = self._ordered_lines()[:1]

    def _remember_sort(self):
        for rec in self:
            self.env['ir.default'].sudo().set(
                self._name, 'sort_by', rec.sort_by, user_id=self.env.uid)

    def write(self, vals):
        res = super().write(vals)
        if vals.get('sort_by'):
            self._remember_sort()
        return res

    def _step(self, step):
        self.ensure_one()
        ids = self._ordered_lines().ids
        if not ids:
            self.line_id = False
        elif self.line_id.id in ids:
            pos = ids.index(self.line_id.id) + step
            self.line_id = ids[max(0, min(pos, len(ids) - 1))]
        else:
            self.line_id = ids[0]
        # Re-open this same session: a falsy return would close the dialog
        # (doActionButton turns it into act_window_close), while a new
        # target-new action replaces it in place.
        return self._action()

    def action_next(self):
        return self._step(1)

    def action_prev(self):
        return self._step(-1)
