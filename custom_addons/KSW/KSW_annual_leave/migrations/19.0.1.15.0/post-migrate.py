# -*- coding: utf-8 -*-
"""Seed the company-wide accounting team from today's group membership.

Until now the accounting step of the time-off chain was resolved by group
membership alone, so every member of `group_annual_leave_acc` could approve
-- and was notified about -- every department's requests. This upgrade moves
the scope onto the record's own data: `hr.department.x_accountant_ids` and
its stored resolver `x_effective_accountant_ids`, falling back to
`res.company.x_default_accountant_ids`.

Seeding matters as much as the fields. With the company team empty, the
first request to reach the accounting step resolves to nobody and stalls
with an error and no approver. So the people who hold the group *today*
become the company-wide team: on the morning after the upgrade the step
behaves exactly as it did the morning before, and HR then narrows it one
department at a time with the chain working throughout.

Narrowing is a two-part edit and the second half is easy to miss: naming
somebody on their one department does NOT remove them from the company
team, and the company team still answers for every other department. To
confine somebody to one department, name them there AND take them out of
Settings > Employees > Accounting Team.

Idempotent: only companies with an empty team are touched, so re-running
never overwrites what HR has since set. Departments are deliberately left
blank -- an empty department inherits the company team, which is the same
answer with less data to maintain.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

ACC_GROUP = 'KSW_annual_leave.group_annual_leave_acc'


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})

    if 'x_default_accountant_ids' not in env['res.company']._fields:
        _logger.warning(
            "KSW_annual_leave: res.company.x_default_accountant_ids is not in "
            "the registry -- upgrade KSW_base_security to 19.0.1.5.0 or later, "
            "then re-run this upgrade. The accounting team was NOT seeded.")
        return

    group = env.ref(ACC_GROUP, raise_if_not_found=False)
    if not group:
        _logger.warning("KSW_annual_leave: %s not found; nothing seeded.",
                        ACC_GROUP)
        return

    # all_user_ids, not user_ids: a member who arrived through an implying
    # group holds the capability just the same and is doing the work today.
    users = group.sudo().all_user_ids
    employees = env['hr.employee'].sudo().with_context(
        active_test=False).search([('user_id', 'in', users.ids)])
    if not employees:
        _logger.warning(
            "KSW_annual_leave: no employee records behind the %d member(s) of "
            "the Accounting Approver group, so the accounting team could not "
            "be seeded. Set it in Settings > Employees > Accounting Team "
            "before any request reaches the accounting step.", len(users))
        return

    companies = env['res.company'].sudo().search(
        [('x_default_accountant_ids', '=', False)])
    if not companies:
        _logger.info(
            "KSW_annual_leave: every company already has an accounting team; "
            "nothing seeded.")
        return

    companies.write({'x_default_accountant_ids': [(6, 0, employees.ids)]})
    _logger.info(
        "KSW_annual_leave: accounting team seeded on %d company/companies "
        "with %d member(s): %s. Narrow it per department on the department "
        "form, and remove anybody who should be confined to one department "
        "from this company-wide list.",
        len(companies), len(employees), ', '.join(employees.mapped('name')))
