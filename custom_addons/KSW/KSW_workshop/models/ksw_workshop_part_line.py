from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class KswWorkshopPartLine(models.Model):
    """One spare part issued to one repair — the whole of the pass-through ledger.

    A line IS the issue: entering it takes the part out of the (notional)
    inventory and puts it on the vehicle in the same act. There is no
    counterpart receipt to reconcile against, which is what "passing item, not
    real stock" means.
    """
    _name = 'ksw.workshop.part.line'
    _description = 'Workshop Spare Part Issued'
    _order = 'issue_date desc, id desc'

    request_id = fields.Many2one(
        'ksw.workshop.request', string='Request', required=True,
        ondelete='cascade', index=True)
    part_id = fields.Many2one('ksw.workshop.part', string='Item', required=True)

    # description and unit_cost are SNAPSHOTS: filled from the item once, then
    # owned by the line. Plain fields, deliberately — not compute/store/
    # readonly=False, the usual idiom for "default it from the m2o". A stored
    # compute is still a compute: anything that re-triggers it silently
    # restates a past repair with today's catalog cost, and a repair's cost
    # must not move after the fact. Same rule as the attendance-line date in
    # KSW_attendance_leave (Odoo 19 Pitfalls #50): a field whose whole job is
    # to survive what happens to the related record cannot be computed
    # through that relation. They are filled by the onchange (in the form) and
    # by create() (every other route), so both entry paths agree.
    description = fields.Char(
        help="Copied from the item when the line was added; edit it to note a variant.")
    quantity = fields.Float(default=1.0, required=True)
    unit_cost = fields.Float(
        help="Copied from the item when the line was added. It stays put if the item's "
             "cost changes later, so a past repair never restates itself.")
    subtotal = fields.Float(compute='_compute_subtotal', store=True)

    # A plain field stamped at creation, never a compute reaching through
    # request_id: a field whose job is to survive the related record being
    # touched must not depend on that relation (Odoo 19 Pitfalls #50).
    issue_date = fields.Date(default=fields.Date.context_today, required=True)

    # Stored mirrors so the Issued Parts pivot has real axes to group on.
    vehicle_id = fields.Many2one(related='request_id.vehicle_id', store=True)
    client_id = fields.Many2one(related='request_id.client_id', store=True)
    technician_id = fields.Many2one(related='request_id.technician_id', store=True)
    state = fields.Selection(related='request_id.state', store=True)

    @api.onchange('part_id')
    def _onchange_part_id(self):
        """Fill the snapshots in the form, without ever overwriting an edit."""
        for line in self:
            if not line.part_id:
                continue
            if not line.description:
                line.description = line.part_id.description
            if not line.unit_cost:
                line.unit_cost = line.part_id.standard_cost

    @api.depends('quantity', 'unit_cost')
    def _compute_subtotal(self):
        for line in self:
            line.subtotal = (line.quantity or 0.0) * (line.unit_cost or 0.0)

    @api.constrains('quantity')
    def _check_quantity(self):
        for line in self:
            if line.quantity <= 0:
                raise ValidationError(_('The quantity issued must be greater than zero.'))

    # ------------------------------------------------------------------
    # CRUD — one guard, reused
    # ------------------------------------------------------------------
    # The parts table is part of the repair report, so it obeys the repair
    # report's rule: workshop manager or technician, only while the request is
    # In Progress. That rule already exists as
    # ksw.workshop.request._check_report_edit_rights(); calling it (rather than
    # restating it) is what keeps the two entry routes — a parent o2m write and
    # a direct write on the line — from drifting apart.
    @api.model_create_multi
    def create(self, vals_list):
        requests = self.env['ksw.workshop.request'].browse(
            [vals['request_id'] for vals in vals_list if vals.get('request_id')]
        )
        requests._check_report_edit_rights()
        # The other half of the snapshot: the onchange only fires in a form,
        # so anything created by RPC, import or another module still gets the
        # item's description and cost stamped on it exactly once.
        Part = self.env['ksw.workshop.part'].sudo()
        for vals in vals_list:
            if not vals.get('part_id') or (vals.get('description') and vals.get('unit_cost')):
                continue
            part = Part.browse(vals['part_id'])
            vals.setdefault('description', part.description)
            if not vals.get('unit_cost'):
                vals['unit_cost'] = part.standard_cost
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
