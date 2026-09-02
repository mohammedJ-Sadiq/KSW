"""Backfill `x_eos_employee_archived` for terminations already carried out.

Reversing an EOS request now restores the employee, but only when the flag
says this request is what archived them — provenance is recorded, never
guessed from the fact that somebody is inactive, or refusing a stale draft
would resurrect an employee HR archived for an unrelated reason.

Requests approved before the flag existed have nothing recorded, so match
them on the two things `action_gm_final_approve` writes and nothing else
does together: the employee is inactive AND their departure date is this
request's start date.  Anything short of both is left alone — an
unrestorable request is a phone call; a wrongly resurrected employee is a
payroll run.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    leaves = env['hr.leave'].with_context(active_test=False).search([
        ('x_is_eos_leave', '=', True),
        ('x_annual_approval_state', 'in',
         ('pending_employee_signature', 'approved')),
        ('state', 'in', ('confirm', 'validate')),
    ])
    owned = leaves.filtered(
        lambda l: l.employee_id
        and not l.employee_id.active
        and l.employee_id.departure_date
        and l.employee_id.departure_date == l.request_date_from
    )
    if owned:
        owned.write({'x_eos_employee_archived': True})
    _logger.info(
        'KSW_eos_leave: %s of %s approved EOS request(s) matched the '
        'employee they archived: %s',
        len(owned), len(leaves), owned.ids,
    )
