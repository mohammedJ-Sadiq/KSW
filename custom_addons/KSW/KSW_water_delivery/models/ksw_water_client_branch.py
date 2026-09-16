from odoo import api, fields, models


class KswWaterClientBranch(models.Model):
    """Which branch delivers to which client.

    Deliberately a pair table rather than a field on the client: 226 of the 319
    credit clients are served by exactly one branch, but 93 are served by two to
    five, so "the client's branch" is not a thing that exists. 462 pairs in all.
    """
    _name = 'ksw.water.client.branch'
    _description = 'Client Served by Branch'
    _order = 'branch_code, partner_id'

    partner_id = fields.Many2one(
        'res.partner', string='Client', required=True, index=True,
        domain="[('customer_rank', '>', 0)]", ondelete='cascade',
    )
    branch_code = fields.Char(string='Branch (CODE2)', required=True, index=True)
    company_id = fields.Many2one(
        'res.company', default=lambda self: self.env.company, required=True,
    )
    active = fields.Boolean(default=True)

    source = fields.Selection(
        [('bas', 'Imported from BAS9'), ('manual', 'Entered in Odoo')],
        default='manual', required=True, readonly=True,
    )
    x_bas_last_delivery = fields.Datetime(string='Last BAS Delivery', readonly=True)

    _partner_branch_uniq = models.Constraint(
        'unique(partner_id, branch_code, company_id)',
        'This client is already listed for that branch.',
    )

    @api.depends('partner_id', 'branch_code')
    def _compute_display_name(self):
        for row in self:
            row.display_name = '%s — %s' % (row.branch_code or '',
                                            row.partner_id.display_name or '')

    @api.model
    def _partner_ids_for_branch(self, branch_code):
        """Clients this branch delivers to. An empty branch code, or a branch
        nobody has listed yet, returns None rather than an empty list -- the
        caller must tell "this branch serves nobody" apart from "nobody has said
        yet", because the second must not lock every driver out."""
        if not branch_code:
            return None
        self.env.cr.execute("""
            SELECT partner_id FROM ksw_water_client_branch
             WHERE active = TRUE AND branch_code = %s AND company_id = %s
        """, (branch_code, self.env.company.id))
        rows = [r[0] for r in self.env.cr.fetchall()]
        return rows or None
