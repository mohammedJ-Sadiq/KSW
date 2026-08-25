from odoo import fields, models


class HelpdeskTicketCategory(models.Model):
    _name = 'helpdesk.ticket.category'
    _description = 'Helpdesk Ticket Category'
    _order = 'ticket_type, sequence, name'

    name = fields.Char(required=True, translate=True)
    ticket_type = fields.Selection([
        ('incident', 'Incident'),
        ('request', 'Service Request'),
    ], required=True, default='incident',
        help="Which ticket type this category applies to. An Incident is "
             "something broken or not working; a Service Request is a "
             "planned ask (new access, new software, information, etc.).",
    )
    sequence = fields.Integer(default=10)
    color = fields.Integer(string='Color')
    icon = fields.Char(help="Font Awesome icon class, e.g. fa-laptop")
    active = fields.Boolean(default=True)

    _name_type_uniq = models.Constraint(
        'unique(name, ticket_type)',
        'A category with this name already exists for this ticket type.',
    )
