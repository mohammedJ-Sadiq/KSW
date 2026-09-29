from odoo import api, models


class HrSalaryRule(models.Model):
    _inherit = 'hr.salary.rule'

    @api.model
    def _ksw_attach_commission_rule(self):
        """Put KSW_COMMISSIONS on every structure that pays vacation
        commissions (see data/salary_rule_commissions.xml)."""
        rule = self.env.ref(
            'KSW_commissions.hr_rule_ksw_commissions', raise_if_not_found=False)
        if not rule:
            return
        structures = self.env['hr.payroll.structure'].sudo().search([
            ('rule_ids.code', '=', 'ADDITIONAL_COMMISSIONS'),
            ('rule_ids', 'not in', rule.ids),
        ])
        if structures:
            structures.write({'rule_ids': [(4, rule.id)]})
