from odoo import fields, models


class HrVersion(models.Model):
    _inherit = 'hr.version'

    mobile_allowance = fields.Monetary(
        string="Mobile Allowance",
        help="Mobile allowance",
    )

    # BAS books it apart from the other allowances, on the collection
    # reps' commission expense (3203020006), not 3201010011.
    collection_commission_allowance = fields.Monetary(
        string="Collection Commission Allowance",
        help="Fixed monthly allowance of a collection representative, paid "
             "on the payslip with the salary.",
    )
