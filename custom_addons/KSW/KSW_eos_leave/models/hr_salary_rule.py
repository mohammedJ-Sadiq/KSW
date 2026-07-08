from odoo import api, models


class HrSalaryRule(models.Model):
    _inherit = 'hr.salary.rule'

    @api.model
    def _ksw_migrate_eos_rules_to_structures(self):
        """Add EOS salary rules to every legacy payroll structure.

        Called once after eos_salary_rules.xml attaches the rules to the
        BASE structure.  Mirrors the pattern of
        ``_ksw_migrate_structures_to_new_system`` in KSW_payroll.
        """
        Structure = self.env['hr.payroll.structure'].sudo()
        eos_codes = ('EOS_AMOUNT', 'EOS_PREV_PAYMENTS', 'EOS_NOTICE_PAY')
        rule_map = {
            r.code: r
            for r in self.env['hr.salary.rule'].sudo().search(
                [('code', 'in', list(eos_codes))])
        }
        if len(rule_map) < 3:
            return True

        for struct_code in ('ATTSHEET', 'Executive', 'NoFin', 'OCIR'):
            struct = Structure.search([('code', '=', struct_code)], limit=1)
            if not struct:
                continue
            existing = {r.code for r in struct.rule_ids}
            to_add = [
                (4, rule_map[c].id)
                for c in eos_codes
                if c in rule_map and c not in existing
            ]
            if to_add:
                struct.write({'rule_ids': to_add})

        return True
