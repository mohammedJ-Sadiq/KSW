# -*- coding: utf-8 -*-
from odoo import models


class ResCompany(models.Model):
    _inherit = 'res.company'

    def write(self, vals):
        res = super().write(vals)
        # Departments with no GM of their own fall back to the company
        # default; changing it moves their open work to the new GM's run.
        if 'x_default_gm_id' in vals:
            self.env['ksw.pay.submission']._rehome_all_open()
        return res
