from odoo import api, fields, models


class StatementCommonWizard(models.AbstractModel):
    """Let the OCA partner statements be opened from a menu.

    Stock partner_statement only works from a partner list's Action menu: it
    reads the partners from ``active_ids``. Its Action entries are gated by
    two Settings toggles that, once on, show them to every internal user,
    while the wizard itself is billing-only. A partner field lets the
    Reporting menu open the dialog directly instead.
    """

    _inherit = 'statement.common.wizard'

    partner_ids = fields.Many2many(
        'res.partner', string='Partners',
        default=lambda self: self._default_partner_ids())

    @api.model
    def _default_partner_ids(self):
        if self.env.context.get('active_model') == 'res.partner':
            return self.env.context.get('active_ids', [])
        return []

    @api.onchange('partner_ids')
    def _onchange_partner_ids_count(self):
        self.number_partner_ids = len(self.partner_ids)

    def _prepare_statement(self):
        if self.partner_ids:
            self = self.with_context(active_ids=self.partner_ids.ids)
        return super(StatementCommonWizard, self)._prepare_statement()
