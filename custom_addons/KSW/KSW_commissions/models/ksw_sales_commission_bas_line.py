"""What one Pull from BAS read, customer by customer.

The sheet's lines say how much each employee sold, collected and had to
collect; these rows say which customers and documents that came from, so a
month can be checked against the accountant's workbook line for line. They
are generated: every pull deletes the sheet's rows and writes them again.
"""
from odoo import fields, models

KIND = [
    ('sales', 'Sales'),
    ('collection', 'Collection'),
    ('target', 'Collection Target'),
]


class KswSalesCommissionBasLine(models.Model):
    _name = 'ksw.sales.commission.bas.line'
    _description = 'Sales & Collection: BAS detail per customer'
    _order = 'kind, employee_id, net desc'

    sheet_id = fields.Many2one(
        'ksw.sales.commission.sheet', required=True, ondelete='cascade',
        index=True)
    kind = fields.Selection(KIND, required=True)
    employee_id = fields.Many2one('hr.employee', string='Credited To')
    status = fields.Selection([
        ('credited', 'Credited'),
        ('house', 'No Commission'),
        ('unmapped', 'Rep Not Linked'),
        ('excluded', 'Left Out (Profile)'),
        ('role', 'Not Their Role'),
    ], required=True)
    commission_type = fields.Selection([
        ('sales', 'Salesman'),
        ('collect', 'Collector'),
        ('both', 'Salesman & Collector'),
    ], string='Type')
    rep_code = fields.Char('BAS Rep Code')
    rep_name = fields.Char('Rep Name (BAS)')
    account = fields.Char('Customer Account')
    customer_name = fields.Char('Customer')
    term_days = fields.Integer('Term (Days)')
    balance = fields.Float('Balance at Month Start', digits=(16, 2),
                           help='VAT included, as BAS holds it.')
    not_due = fields.Float('Not Yet Due', digits=(16, 2),
                           help='Invoices within the payment term, VAT '
                                'included.')
    gross = fields.Float('Amount incl. VAT', digits=(16, 2))
    net = fields.Float('Amount excl. VAT', digits=(16, 2))
    doc_count = fields.Integer('Documents')
