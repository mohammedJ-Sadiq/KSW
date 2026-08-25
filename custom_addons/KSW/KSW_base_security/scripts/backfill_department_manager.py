# -*- coding: utf-8 -*-
"""Propose (and, on confirmation, write) `hr.department.manager_id`.

`manager_id` is empty on 25 of 27 departments. It is what the commissions
supervisor scoping keys on, so until it is filled a supervisor correctly
sees nothing and cannot record overtime or allowances at all.

There is no authoritative list of department managers anywhere in the
database, but there is a strong signal: the direct manager
(`hr.employee.parent_id`) that most of a department's employees already
report to. On the two departments that DO have a manager set today, this
derivation reproduces it exactly -- which is the only evidence available
that the method is sound.

A derived manager is a proposal, not a fact. So the script prints the table
and stops. Nothing is written until it is run again with --apply.

    # propose
    python odoo-bin shell -c KSW_dev.conf --http-port=18070 \
        < custom_addons/KSW/KSW_base_security/scripts/backfill_department_manager.py

    # apply, after reading the table
    KSW_APPLY=1 python odoo-bin shell -c KSW_dev.conf --http-port=18070 \
        < custom_addons/KSW/KSW_base_security/scripts/backfill_department_manager.py

Only departments with no manager are ever touched, so re-running is safe.
"""
import os
from collections import Counter

APPLY = os.environ.get('KSW_APPLY') == '1'


def propose(env):
    """Return [(department, proposed_manager, agreeing, total)] sorted by size."""
    Department = env['hr.department'].sudo()
    Employee = env['hr.employee'].sudo()

    rows = []
    for dept in Department.search([]):
        employees = Employee.search([('department_id', '=', dept.id)])
        if not employees:
            rows.append((dept, Employee.browse(), 0, 0))
            continue
        # A manager who sits in the department cannot be its own manager's
        # proposal source only if he reports outside it -- which is the
        # normal shape, so no filtering here. Self-managed employees
        # (parent_id == self) are excluded: they would nominate themselves.
        counter = Counter(
            emp.parent_id.id for emp in employees
            if emp.parent_id and emp.parent_id != emp
        )
        if not counter:
            rows.append((dept, Employee.browse(), 0, len(employees)))
            continue
        manager_id, agreeing = counter.most_common(1)[0]
        rows.append((dept, Employee.browse(manager_id), agreeing, len(employees)))

    rows.sort(key=lambda r: -r[3])
    return rows


def main(env):
    rows = propose(env)

    print()
    print('%-30s %-38s %8s %8s %s' % (
        'DEPARTMENT', 'PROPOSED MANAGER', 'AGREEING', 'STAFF', 'CURRENT'))
    print('-' * 110)
    to_write = []
    for dept, manager, agreeing, total in rows:
        current = dept.manager_id.name or ''
        if dept.manager_id:
            status = 'keep: %s' % current
        elif not manager:
            status = 'SKIP (no signal)'
        else:
            status = 'WRITE'
            to_write.append((dept, manager))
        print('%-30s %-38s %8s %8s %s' % (
            (dept.complete_name or dept.name)[:30],
            (manager.name or '-')[:38], agreeing or '-', total or '-', status))

    print('-' * 110)
    print('%d department(s) would be written, %d already set, %d skipped.' % (
        len(to_write),
        sum(1 for r in rows if r[0].manager_id),
        sum(1 for r in rows if not r[0].manager_id and not r[1]),
    ))

    if not APPLY:
        print()
        print('Nothing written. Re-run with KSW_APPLY=1 to apply.')
        return

    for dept, manager in to_write:
        dept.write({'manager_id': manager.id})
    env.cr.commit()
    print()
    print('Applied to %d department(s) and committed.' % len(to_write))


main(env)  # noqa: F821 - `env` is injected by `odoo-bin shell`
