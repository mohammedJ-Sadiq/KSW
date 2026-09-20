from odoo import _, api, fields, models
from odoo.exceptions import UserError


class ItConsumablePurchaseRequest(models.Model):
    _name = 'it.consumable.purchase.request'
    _description = 'IT Consumable Purchase Request'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'date_request desc, id desc'

    name = fields.Char(required=True, copy=False, readonly=True, default='New')
    date_request = fields.Date(default=fields.Date.context_today, required=True)
    requested_by = fields.Many2one('res.users', default=lambda self: self.env.user, readonly=True)
    approver_id = fields.Many2one(
        'hr.employee', string='To Be Signed By',
        help="Manager expected to approve this request. Informational only "
             "- printed on the PDF for the physical signature.",
    )
    state = fields.Selection([
        ('draft', 'Draft'),
        ('purchased', 'Purchased'),
        ('cancelled', 'Cancelled'),
    ], default='draft', required=True, tracking=True)
    note = fields.Text(string='Notes for Supply Chain')
    line_ids = fields.One2many(
        'it.consumable.purchase.request.line', 'request_id', string='Items',
    )
    currency_id = fields.Many2one(related='company_id.currency_id')
    amount_total = fields.Monetary(
        compute='_compute_amount_total', store=True, currency_field='currency_id',
    )
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.depends('line_ids.subtotal')
    def _compute_amount_total(self):
        for request in self:
            request.amount_total = sum(request.line_ids.mapped('subtotal'))

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code(
                    'it.consumable.purchase.request') or 'New'
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def action_print(self):
        return self.env.ref(
            'KSW_helpdesk.action_report_it_consumable_purchase_request'
        ).report_action(self)

    def action_mark_purchased(self):
        for request in self:
            if request.state != 'draft':
                raise UserError(_('Only a draft request can be marked as purchased.'))
            if not request.line_ids:
                raise UserError(_(
                    'Add at least one item before marking this request as purchased.'))
            for line in request.line_ids.filtered(lambda l: l.quantity > 0):
                self.env['it.consumable.move'].create({
                    'consumable_id': line.consumable_id.id,
                    'move_type': 'in',
                    'quantity': line.quantity,
                    'note': _('Received via Purchase Request %s', request.name),
                })
            request.state = 'purchased'

    def action_cancel(self):
        self.filtered(lambda r: r.state == 'draft').write({'state': 'cancelled'})

    def action_reset_to_draft(self):
        self.filtered(lambda r: r.state == 'cancelled').write({'state': 'draft'})
