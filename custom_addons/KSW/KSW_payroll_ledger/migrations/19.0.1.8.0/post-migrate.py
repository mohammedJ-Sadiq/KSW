"""Leave settlements in the ledger, and paying them (Nov 2026).

Configuration only: the settlement accounts, the paying accounts' bank
journals, Arab Bank's ledger account (hooks.build_settlement_config, which
fills what is empty and never overwrites). Booking the settlements already
confirmed is not done here: it writes journal entries, so it is run by hand
at deploy (``ksw.payroll.ledger._rebook_settlements``), after review.
"""
from odoo import SUPERUSER_ID, api

from odoo.addons.KSW_payroll_ledger.hooks import build_settlement_config


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for company in env['res.company'].search([]):
        build_settlement_config(env, company)
