"""Retire the l10n_sa template accounts BAS has no counterpart for.

Runs after this version's data, so every bank journal already posts straight
to its BAS account.  Repoint first, then archive (never delete).  Guarded on
the BAS chart being present, so a database still on plain l10n_sa is untouched.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

TEMPLATE_CODES = [
    '100001',  # Liquidity Transfer
    '100103',  # VAT Receivable (tax closing, Enterprise only)
    '100104',  # Withholding Receivable
    '101001',  # Bank
    '101002',  # Bank Suspense
    '101003',  # Outstanding Receipts
    '101004',  # Outstanding Payments
    '102011',  # Accounts Receivable (default for partners)
    '102012',  # Accounts Receivable (PoS)
]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {'active_test': False})
    Account = env['account.account']
    for company in env['res.company'].search([]):
        Account = Account.with_company(company)
        if not Account.search_count([
                *Account._check_company_domain(company),
                ('code', '=like', '1202%'), ('account_type', '=', 'asset_cash')]):
            continue
        accounts = Account.search([
            *Account._check_company_domain(company), ('code', 'in', TEMPLATE_CODES)])
        if not accounts:
            continue
        if company.transfer_account_id in accounts:
            company.transfer_account_id = False
        if company.account_journal_suspense_account_id in accounts:
            company.account_journal_suspense_account_id = False
        env['account.journal'].search([
            ('company_id', '=', company.id), ('suspense_account_id', 'in', accounts.ids),
        ]).suspense_account_id = False
        env['account.reconcile.model'].search([
            ('company_id', '=', company.id), ('line_ids.account_id', 'in', accounts.ids),
        ]).active = False
        groups = env['account.tax.group'].search([('company_id', '=', company.id)])
        groups.filtered(lambda g: g.tax_receivable_account_id in accounts).tax_receivable_account_id = False
        env['ir.default'].search([
            ('field_id.model', '=', 'res.partner'),
            ('field_id.name', '=', 'property_account_receivable_id'),
            ('company_id', '=', company.id),
            ('json_value', 'in', [str(i) for i in accounts.ids]),
        ]).unlink()
        accounts.filtered('active').active = False
        _logger.info('Retired template accounts for %s: %s', company.name, accounts.mapped('code'))
