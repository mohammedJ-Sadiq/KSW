# -*- coding: utf-8 -*-
from odoo import api, fields, models


class ResCompany(models.Model):
    """Company-wide fallback General Manager.

    Not a nicety: 103 active employees have no department at all, and their
    requests still have to reach somebody. This is also the seed value every
    department starts from, so the chains keep working the day the feature
    ships and HR can then set the real per-department GMs at its own pace.

    `hr.department.x_effective_gm_id` lists `company_id.x_default_gm_id` in
    its `@api.depends`, so writing here recomputes every department that was
    falling back to it -- no explicit invalidation needed.
    """
    _inherit = 'res.company'

    x_default_gm_id = fields.Many2one(
        'hr.employee',
        string='Default General Manager',
        help="Approves the GM step for employees whose department has no GM "
             "of its own (and for employees with no department at all).",
    )

    def _ksw_sync_default_gm_capability(self):
        """The company GM needs the same capability groups as any other.

        He is a GM for real records -- every department with no GM of its
        own, plus the 103 employees with no department at all -- but he is
        reached through this field rather than through `hr.department`, so
        the department's own create/write hook never sees him. Without this
        the whole fallback is dead: the guards resolve to him and the record
        rules, which are keyed on the groups, show him nothing.
        """
        self.env['hr.department']._ksw_grant_gm_capability(
            self.mapped('x_default_gm_id'))

    @api.model_create_multi
    def create(self, vals_list):
        companies = super().create(vals_list)
        companies.filtered('x_default_gm_id')._ksw_sync_default_gm_capability()
        return companies

    def write(self, vals):
        res = super().write(vals)
        if 'x_default_gm_id' in vals:
            self.filtered('x_default_gm_id')._ksw_sync_default_gm_capability()
        return res
