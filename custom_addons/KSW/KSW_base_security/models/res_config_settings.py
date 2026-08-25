# -*- coding: utf-8 -*-
from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    x_default_gm_id = fields.Many2one(
        'hr.employee',
        related='company_id.x_default_gm_id',
        readonly=False,
        string='Default General Manager',
        help="Approves the GM step for employees whose department has no GM "
             "of its own (and for employees with no department at all).",
    )
