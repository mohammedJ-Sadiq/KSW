from odoo import fields, models


class SpreadsheetDashboard(models.Model):
    """A Dashboards-app entry that shows one of the KSW overview pages instead of
    a spreadsheet, so the Dashboards app is the one place every dashboard lives.

    The entry carries no spreadsheet data. The client patch in static/src/hub
    renders the client action in place of the spreadsheet. Who sees the entry is
    the standard group_ids rule (ir_rule_spreadsheet_dashboard), set to the same
    groups that open the overview's own menu.
    """
    _inherit = 'spreadsheet.dashboard'

    x_client_action_id = fields.Many2one('ir.actions.client', string='Overview Page', ondelete='cascade')
    x_client_action_tag = fields.Char(related='x_client_action_id.tag')
