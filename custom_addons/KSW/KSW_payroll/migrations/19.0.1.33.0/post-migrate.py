"""Collection Commission Allowance: its BAS account for the voucher export.

The rule's record is reloaded on every update, so the account code is set
here once, and only where nobody has entered one: after this it is the
accountant's, like every other rule's.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE hr_salary_rule r
           SET x_bas_account_code = '3203020006'
          FROM ir_model_data d
         WHERE d.model = 'hr.salary.rule' AND d.res_id = r.id
           AND d.module = 'KSW_payroll' AND d.name = 'hr_rule_collection_commission'
           AND COALESCE(r.x_bas_account_code, '') = ''
    """)
    _logger.info('KSW_payroll: Collection Commission Allowance BAS account set on %s rule(s)', cr.rowcount)
