"""Collection Commission Allowance (KSW_payroll 19.0.1.33.0) to 3203020006.

BAS books it on the collection reps' commission expense, whatever the
employee's branch: a Fixed Ledger Account, set only where the rule has none,
so an accountant's later choice is never overwritten.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    rule = env.ref('KSW_payroll.hr_rule_collection_commission', raise_if_not_found=False)
    if not rule or rule.x_ledger_account_id:
        return
    account = env['account.account'].search([('code', '=', '3203020006')], limit=1)
    if account:
        rule.x_ledger_account_id = account
    _logger.info('KSW_payroll_ledger: Collection Commission Allowance → %s', account.code or 'no account')
