# -*- coding: utf-8 -*-
"""Seed the department-level GM.

Until now every GM approval step was resolved by group membership, so the
whole company effectively had one GM. This upgrade introduces
`hr.department.x_gm_id` and its stored resolver `x_effective_gm_id`.

Seeding matters as much as the field: on the first request after the
upgrade, a department with no GM and no company default resolves to nobody,
and the request stalls with no approver and no notification. So set the
company default to the sitting GM and stamp every manager-less department
with it. HR then edits the real per-department GMs on the department form,
one at a time, with the chains working throughout.

Idempotent: only departments with no `x_gm_id` are touched, so re-running
this never overwrites what HR has since set.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

# Resolved in order. Login first: `res.users` is the one identity that is
# the same record on odoo_dev and KSWCO, whereas employee ids differ between
# the two databases and must never be hardcoded.
GM_LOGIN = 'm.shaibani@alkawthersw.com'
GM_SSNID = '2023599638'
GM_EMPLOYEE_NO = '101'


def _find_gm(env):
    Employee = env['hr.employee'].sudo().with_context(active_test=False)

    user = env['res.users'].sudo().with_context(active_test=False).search(
        [('login', '=', GM_LOGIN)], limit=1)
    if user:
        employee = Employee.search([('user_id', '=', user.id)], limit=1)
        if employee:
            return employee

    employee = Employee.search([('ssnid', '=', GM_SSNID)], limit=1)
    if employee:
        return employee

    return Employee.search([('x_employee_no', '=', GM_EMPLOYEE_NO)], limit=1)


def migrate(cr, version):
    if not version:
        return

    env = api.Environment(cr, SUPERUSER_ID, {})
    gm = _find_gm(env)
    if not gm:
        _logger.warning(
            "KSW_base_security: no General Manager found for login %s / "
            "ssnid %s / employee no %s. Set the Default General Manager in "
            "Settings > Employees before any GM approval step is reached.",
            GM_LOGIN, GM_SSNID, GM_EMPLOYEE_NO)
        return
    if not gm.user_id:
        _logger.warning(
            "KSW_base_security: %s has no linked user account and cannot "
            "act as a General Manager. Skipping the seed.", gm.name)
        return

    companies = env['res.company'].sudo().search(
        [('x_default_gm_id', '=', False)])
    if companies:
        companies.write({'x_default_gm_id': gm.id})
        _logger.info(
            "KSW_base_security: default General Manager set to %s on %d "
            "company/companies.", gm.name, len(companies))

    departments = env['hr.department'].sudo().with_context(
        active_test=False).search([('x_gm_id', '=', False)])
    if departments:
        departments.write({'x_gm_id': gm.id})
        _logger.info(
            "KSW_base_security: %s seeded as General Manager on %d "
            "department(s). HR can now set the real GM per department.",
            gm.name, len(departments))
