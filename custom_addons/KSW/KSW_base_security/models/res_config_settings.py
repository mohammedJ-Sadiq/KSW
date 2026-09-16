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

    x_default_accountant_ids = fields.Many2many(
        'hr.employee',
        relation='ksw_company_default_accountant_rel',
        column1='company_id', column2='employee_id',
        related='company_id.x_default_accountant_ids',
        readonly=False,
        string='Accounting Team',
        help="May approve the accounting step of time off for employees "
             "whose department has no Accounting Approvers of its own (and "
             "for employees with no department at all).",
    )
