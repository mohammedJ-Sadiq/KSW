from odoo import api, fields, models


class KswWorkshopLaborLine(models.Model):
    """One labor / service item on a repair: what was done, and by whom.

    No money on the line, by decision (2026-09-14): the labor / service fee
    stays a single figure on the request, because what the workshop wanted
    itemised was the work and the person, not a per-task price. If a per-line
    amount is ever wanted, add it here and make request.labor_cost the sum —
    do not start a second place to type a total.
    """
    _name = 'ksw.workshop.labor.line'
    _description = 'Workshop Labor / Service Line'
    _order = 'sequence, id'

    request_id = fields.Many2one(
        'ksw.workshop.request', string='Request', required=True,
        ondelete='cascade', index=True)
    sequence = fields.Integer(default=10)
    line_no = fields.Integer(
        string='#', compute='_compute_line_no',
        help="Position of this line on the repair. Follows the order of the table.")
    description = fields.Char(required=True)
    technician_id = fields.Many2one(
        'ksw.workshop.technician', string='Technician', ondelete='restrict',
        help="A workshop technician, or External Service Location when the job was "
             "sent outside.")

    @api.depends('sequence', 'request_id.labor_line_ids', 'request_id.labor_line_ids.sequence')
    def _compute_line_no(self):
        for line in self:
            # Sorted the same way _order does, but in Python, so an unsaved
            # line in an open form is numbered too. NewId has no integer to
            # sort on, hence the guard.
            siblings = line.request_id.labor_line_ids.sorted(
                lambda sibling: (
                    sibling.sequence or 0,
                    sibling.id if isinstance(sibling.id, int) else 0,
                ))
            line.line_no = list(siblings).index(line) + 1 if line in siblings else 1

    # ------------------------------------------------------------------
    # CRUD — one guard, reused
    # ------------------------------------------------------------------
    # Labor lines are part of the repair report, so they obey the repair
    # report's rule: workshop manager or technician, only while the request is
    # In Progress. Calling ksw.workshop.request._check_report_edit_rights()
    # rather than restating it is what keeps a parent o2m write and a direct
    # write on the line from drifting apart — the same arrangement as
    # ksw.workshop.part.line.
    @api.model_create_multi
    def create(self, vals_list):
        requests = self.env['ksw.workshop.request'].browse(
            [vals['request_id'] for vals in vals_list if vals.get('request_id')]
        )
        requests._check_report_edit_rights()
        return super().create(vals_list)

    def write(self, vals):
        self.request_id._check_report_edit_rights()
        if vals.get('request_id'):
            self.env['ksw.workshop.request'].browse(
                vals['request_id'])._check_report_edit_rights()
        return super().write(vals)

    def unlink(self):
        self.request_id._check_report_edit_rights()
        return super().unlink()
