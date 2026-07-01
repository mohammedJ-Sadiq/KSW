from odoo import api, models


class HrSalaryRule(models.Model):
    _inherit = 'hr.salary.rule'

    @api.model
    def _ksw_migrate_structures_to_new_system(self):
        """Bring the four legacy payroll structures up to the BASE feature set.

        Runs after vacation_salary_rules.xml so all new rules are already
        attached to the base structure before we copy them out.

        Per-structure plan
        ------------------
        ATTSHEET  — remove MISDAYS/SCLINS/LO; add all new BASE rules inc. ATTDED/SATOT
        Executive — remove ABSENCE_DED/SCLINS/LO/UNPAIDVAC; add all new BASE rules inc. ATTDED/SATOT
        NoFin     — remove SCLINS/LO/UNPAIDVAC; add new BASE rules except ATTDED/SATOT
                    (no biometric or sheet tracking; ATTDED would deduct everything,
                    and SATOT only ever fires for attendance-sheet employees)
        OCIR      — remove ABSENCE_DED/SCLINS/LO; add all new BASE rules inc. ATTDED/SATOT
        """
        Structure = self.env['hr.payroll.structure'].sudo()
        Rule = self.env['hr.salary.rule'].sudo()

        # All salary rules indexed by code for quick lookup
        rule_map = {r.code: r for r in Rule.search([])}

        # Rules to add to most structures (all are in BASE already)
        _NEW = [
            'DA', 'Travel', 'Meal', 'Medical', 'Mobile',
            'VACATION_BAL', 'FLIGHT_TICKET', 'ADDITIONAL_COMMISSIONS',
            'VACATION_HRA', 'ATTDED', 'SATOT', 'GOSI', 'VACATION_GOSI',
            'PENALTY', 'KSW_DEDUCTIONS', 'REMAINING_LOANS',
            'FIN_CONSIDERATION', 'VISA_COST_RECOVERY',
        ]
        _NEW_NO_ATTDED = [c for c in _NEW if c not in ('ATTDED', 'SATOT')]

        STRUCT_PLAN = {
            'ATTSHEET':  {'remove': {'MISDAYS', 'SCLINS', 'LO'},
                          'add': _NEW},
            'Executive': {'remove': {'ABSENCE_DED', 'SCLINS', 'LO', 'UNPAIDVAC'},
                          'add': _NEW},
            'NoFin':     {'remove': {'SCLINS', 'LO', 'UNPAIDVAC'},
                          'add': ['HRA'] + _NEW_NO_ATTDED},
            'OCIR':      {'remove': {'ABSENCE_DED', 'SCLINS', 'LO'},
                          'add': _NEW},
        }

        for code, plan in STRUCT_PLAN.items():
            struct = Structure.search([('code', '=', code)], limit=1)
            if not struct:
                continue

            current_codes = {r.code for r in struct.rule_ids}

            # Remove legacy rules
            to_remove = struct.rule_ids.filtered(
                lambda r, rm=plan['remove']: r.code in rm)
            if to_remove:
                struct.write({'rule_ids': [(3, r.id) for r in to_remove]})

            # Add new rules that are not already present
            to_add = [
                (4, rule_map[c].id)
                for c in plan['add']
                if c in rule_map and c not in current_codes
            ]
            if to_add:
                struct.write({'rule_ids': to_add})

        return True

    @api.model
    def _ksw_sync_legacy_absence_deduction_rule(self):
        """Normalize legacy absence-deduction salary rules so they work with
        the current KSW worked-day codes.

        Two rules are patched:
        - ABSENCE_DED: old rule attached to legacy structures; used to read
          TOTDAYS which no longer exists.
        - MISDAYS (salary rule): legacy "Missed days(ATT SHEET)" rule that
          read worked_days.WORK100.number_of_days — WORK100 no longer exists
          so the attribute access on the float 0.0 crashes.

        Both are rewritten to read the monetary total from the MISDAYS
        worked-day line (populated by KSW as a backward-compatibility alias
        for ATT_DED).
        """
        _new_condition = "result = bool(worked_days.MISDAYS)"
        _new_amount = "result = -worked_days.MISDAYS.amount if worked_days.MISDAYS else 0.0"

        for code in ('ABSENCE_DED', 'MISDAYS'):
            rule = self.sudo().search([('code', '=', code)], limit=1)
            if not rule:
                continue
            rule.write({
                'condition_select': 'python',
                'condition_python': _new_condition,
                'amount_select': 'code',
                'amount_python_compute': _new_amount,
            })
        return True



