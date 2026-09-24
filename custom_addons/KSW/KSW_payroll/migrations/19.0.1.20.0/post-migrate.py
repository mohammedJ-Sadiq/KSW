"""Give every existing salary rule its side of the BAS journal entry.

Only the *side*. See ``hr.salary.rule._ksw_classify_bas_posting`` — the
account numbers are KSW's chart and stay for the accountant to fill in on
each rule. Until he does, the journal-entry export stops and names the
rule rather than posting an entry that pays somebody a different net than
his payslip says.

Idempotent: a rule already classified is never re-classified.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    counts = env['hr.salary.rule']._ksw_classify_bas_posting()
    _logger.info(
        'KSW_payroll: BAS posting classified on salary rules %s. The '
        'account codes themselves still have to be filled in per rule.',
        counts or 'none')
