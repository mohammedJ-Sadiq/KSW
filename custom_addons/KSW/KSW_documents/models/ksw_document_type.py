from odoo import fields, models


class KswDocumentType(models.Model):
    """What a document is and who must have one. Configuration: a new kind
    of permit is a record here, not code."""
    _name = 'ksw.document.type'
    _description = 'Document Type'
    _order = 'applies_to, sequence, id'

    name = fields.Char(required=True, translate=True)
    code = fields.Char(required=True, help='Stable key used by the code and the AI reader.')
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    applies_to = fields.Selection([('vehicle', 'Vehicle'), ('employee', 'Employee'), ('company', 'Company')],
                                  required=True)
    required_for = fields.Selection([
        ('none', 'Nobody (optional)'),
        ('own_fleet', 'Every company vehicle'),
        ('public_transport', 'Public-transport company vehicles (نقل عام)'),
        ('water_tankers', 'Water tankers (trailer heads and Isuzu tankers)'),
        ('custodians', 'Employees who hold a vehicle'),
        ('non_saudi', 'Non-Saudi employees'),
        ('all', 'Every employee'),
    ], string='Required For', required=True, default='none')
    has_expiry = fields.Boolean(default=True)
    # The istimara card no longer prints its expiry (it is in Absher): it
    # is confirmed without one, and the date is asked for until HR adds it.
    expiry_on_document = fields.Boolean(string='Expiry Printed on It', default=True)
    has_class = fields.Boolean(string='Has a Class', help='e.g. a driving licence: private, heavy, public.')
    alert_days = fields.Integer(string='Alert Days Before Expiry', default=30)
    # An employee document Odoo already has fields for: the register writes
    # them back on confirmation, so it stays the only place to enter them.
    sync_number_field = fields.Char(help='hr.employee field that receives the number.')
    sync_expiry_field = fields.Char(help='hr.employee field that receives the expiry date.')
    ai_hint = fields.Char(string='How It Looks', help='Told to the AI reader, e.g. its Arabic title.')

    _code_uniq = models.Constraint('unique(code)', 'The code must be unique.')
