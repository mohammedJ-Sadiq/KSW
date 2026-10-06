from odoo import fields, models


class KswPlWizard(models.TransientModel):
    """Ask for a date range, then open the Income Statement for exactly it."""

    _name = 'ksw.pl.wizard'
    _inherit = 'ksw.mis.period.wizard'
    _description = 'Profit and Loss: choose period'

    layout = fields.Selection([
        ('summary', 'Summary'),
        ('account', 'By account'),
        ('monthly', 'Month by month'),
    ], required=True, default='summary')

    def action_open(self):
        return self._open_statement('KSW_accounting_ux.mis_pl_report', self.env._('Profit and Loss'))
