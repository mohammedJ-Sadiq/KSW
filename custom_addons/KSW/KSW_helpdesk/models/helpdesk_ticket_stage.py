from odoo import fields, models


class HelpdeskTicketStage(models.Model):
    _name = 'helpdesk.ticket.stage'
    _description = 'Helpdesk Ticket Stage'
    _order = 'sequence, id'

    name = fields.Char(required=True, translate=True)
    sequence = fields.Integer(default=10)
    fold = fields.Boolean(help="This stage is folded in the kanban view when there are no records.")
    is_closed = fields.Boolean(help="Tickets in this stage are considered resolved/closed.")
    description = fields.Text(translate=True)
