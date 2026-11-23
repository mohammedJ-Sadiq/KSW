from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .ksw_document import OFFICER_GROUP


class KswDocumentRequest(models.Model):
    """Someone outside HR needs a document in the register: an employee's
    (a new driver's licence) or a vehicle's (its insurance before it changes
    hands). The request is his: he follows it without reading the register.
    HR registers the document; confirming it closes every open request for
    that owner and document, wherever it was entered."""
    _name = 'ksw.document.request'
    _description = 'Document Request'
    _inherit = ['mail.thread']
    _order = 'state, create_date, id'

    name = fields.Char(readonly=True, copy=False, default='/')
    type_id = fields.Many2one('ksw.document.type', string='Document', required=True, readonly=True,
                              domain="[('applies_to', 'in', ('employee', 'vehicle'))]")
    applies_to = fields.Selection(related='type_id.applies_to')
    employee_id = fields.Many2one('hr.employee', string='Employee', readonly=True, index=True)
    vehicle_id = fields.Many2one('ksw.fleet.vehicle', string='Vehicle', readonly=True, index=True)
    requested_by_id = fields.Many2one('res.users', string='Requested By', required=True, readonly=True,
                                      default=lambda self: self.env.user, index=True)
    reason = fields.Char(required=True, readonly=True, help='What waits for the document.')
    origin = fields.Char(string='Source', readonly=True)
    state = fields.Selection([('requested', 'Waiting for HR'), ('done', 'Registered'), ('cancelled', 'Cancelled')],
                             default='requested', required=True, readonly=True, tracking=True, index=True)
    document_id = fields.Many2one('ksw.document', string='Registered Document', readonly=True, copy=False)
    done_date = fields.Datetime(string='Registered On', readonly=True, copy=False)
    x_is_pending_my_action = fields.Boolean(compute='_compute_is_pending_my_action',
                                            search='_search_is_pending_my_action', string='Waiting for Me')
    x_can_register = fields.Boolean(compute='_compute_permissions')
    x_can_cancel = fields.Boolean(compute='_compute_permissions')

    @api.model
    def _is_officer(self):
        return self.env.su or self.env.user.has_group(OFFICER_GROUP)

    @api.model
    def _pending_domain(self):
        if not self._is_officer():
            return Domain([('id', '=', False)])
        return Domain([('state', '=', 'requested')])

    @api.depends_context('uid')
    def _compute_is_pending_my_action(self):
        mine = set(self.search(self._pending_domain()).ids)
        for request in self:
            request.x_is_pending_my_action = request.id in mine

    def _search_is_pending_my_action(self, operator, value):
        if operator in ('in', 'not in'):
            wanted = (operator == 'in') == any(value)
        elif operator in ('=', '!='):
            wanted = (operator == '=') == bool(value)
        else:
            return NotImplemented
        domain = self._pending_domain()
        return domain if wanted else ~domain

    @api.depends_context('uid')
    @api.depends('state', 'requested_by_id')
    def _compute_permissions(self):
        officer = self._is_officer()
        for request in self:
            open_ = request.state == 'requested'
            request.x_can_register = open_ and officer
            request.x_can_cancel = open_ and (officer or request.requested_by_id == self.env.user)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals['name'] = self.env['ir.sequence'].sudo().next_by_code('ksw.document.request') or '/'
        requests = super().create(vals_list)
        for request in requests:
            owner = request._owner()
            if bool(request.employee_id) == bool(request.vehicle_id) or owner._name != {
                    'employee': 'hr.employee', 'vehicle': 'ksw.fleet.vehicle'}.get(request.type_id.applies_to):
                raise UserError(_('A document request is for one employee or one vehicle, as its document is.'))
            if self.sudo().search_count([('id', '!=', request.id), ('state', '=', 'requested'),
                                         (request._owner_field(), '=', owner.id),
                                         ('type_id', '=', request.type_id.id)], limit=1):
                raise UserError(_('HR already has a request for the %(t)s of %(e)s.',
                                  t=request.type_id.name, e=owner.sudo().name))
            if request._valid_document():
                raise UserError(_('The %(t)s of %(e)s is already registered and valid.',
                                  t=request.type_id.name, e=owner.sudo().name))
        return requests

    def _owner_field(self):
        self.ensure_one()
        return 'vehicle_id' if self.vehicle_id else 'employee_id'

    def _owner(self):
        self.ensure_one()
        return self.vehicle_id or self.employee_id

    def _valid_document(self):
        self.ensure_one()
        doc = self.env['ksw.document'].sudo().search([
            (self._owner_field(), '=', self._owner().id), ('type_id', '=', self.type_id.id),
            ('state', '=', 'confirmed')], limit=1)
        if doc and doc.expiry_date and doc.expiry_date < fields.Date.context_today(self):
            return doc.browse()
        return doc

    @api.model
    def _ksw_open_for(self, owner, doc_type):
        """The open request for this employee's or vehicle's document, if any."""
        field = 'vehicle_id' if owner._name == 'ksw.fleet.vehicle' else 'employee_id'
        return self.sudo().search([(field, '=', owner.id), ('type_id', '=', doc_type.id),
                                   ('state', '=', 'requested')], limit=1)

    def action_register(self):
        """HR: the draft to fill in, already pointing at the employee."""
        self.ensure_one()
        if not self.x_can_register:
            raise UserError(_('Only HR registers the document of %s.', self.name))
        field, owner = self._owner_field(), self._owner()
        doc = self.env['ksw.document'].search([
            (field, '=', owner.id), ('type_id', '=', self.type_id.id), ('state', '=', 'draft')], limit=1)
        if not doc:
            doc = self.env['ksw.document'].create({'type_id': self.type_id.id, 'applies_to': self.type_id.applies_to,
                                                   field: owner.id})
            doc.message_post(body=_('Requested by %(u)s (%(r)s): %(n)s', u=self.requested_by_id.name,
                                    r=self.name, n=self.reason), subtype_xmlid='mail.mt_note')
        return {'type': 'ir.actions.act_window', 'res_model': 'ksw.document', 'res_id': doc.id,
                'view_mode': 'form', 'target': 'current'}

    def action_cancel(self):
        for request in self:
            if not request.x_can_cancel:
                raise UserError(_('%s can be cancelled by its requester or HR while it waits.', request.name))
        self.sudo().write({'state': 'cancelled'})
        return True

    def _ksw_close_with(self, doc):
        """A confirmed document answers the open requests for it."""
        for request in self.sudo():
            request.write({'state': 'done', 'document_id': doc.id, 'done_date': fields.Datetime.now()})
            request.message_post(
                body=_('HR registered %(d)s. %(n)s can go ahead.', d=doc.name, n=request.reason),
                partner_ids=request.requested_by_id.partner_id.ids, subtype_xmlid='mail.mt_comment')
