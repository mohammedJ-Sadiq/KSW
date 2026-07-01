# -*- coding: utf-8 -*-
"""
Pre-migration: remove stale rule_group_rel entries for old KSW_annual_leave
groups that were deleted when the security model was refactored from four
orthogonal CRUD privileges (Creation / Viewing / Modification / Deletion,
~12 groups) into one tiered "Leave Management" privilege (4 groups).

PostgreSQL RESTRICT on rule_group_rel.group_id prevents deletion of res.groups
records that are still referenced by record rules.  This script clears those
FK entries *before* Odoo processes the new security.xml, so the old orphaned
groups can be deleted cleanly.
"""


def migrate(cr, version):
    if not version:
        return

    # Old group xmlnames that no longer exist in the new security.xml.
    # We keep the seven current groups and drop everything else from this module.
    current_xmlnames = {
        'group_leave_self',
        'group_leave_supervisor',
        'group_leave_supervisor_cascading',
        'group_leave_officer',
        'group_annual_leave_hr',
        'group_annual_leave_acc',
        'group_annual_leave_gm',
    }

    # Find all res.groups IDs that KSW_annual_leave owns but that are NOT in
    # the new security.xml — these are the orphaned old groups.
    cr.execute("""
        SELECT res_id
        FROM   ir_model_data
        WHERE  module = 'KSW_annual_leave'
          AND  model  = 'res.groups'
          AND  name   NOT IN %s
    """, (tuple(current_xmlnames),))
    old_group_ids = [row[0] for row in cr.fetchall()]

    if not old_group_ids:
        return

    # Remove their entries from rule_group_rel so the groups can be deleted.
    cr.execute("""
        DELETE FROM rule_group_rel
        WHERE group_id = ANY(%s)
    """, (old_group_ids,))

    # Also remove from res_groups_implied_rel (group implication many2many) so
    # the groups can be deleted without violating that FK either.
    cr.execute("""
        DELETE FROM res_groups_implied_rel
        WHERE hid = ANY(%s) OR gid = ANY(%s)
    """, (old_group_ids, old_group_ids))

    # Remove from res_groups_users_rel (user membership).
    cr.execute("""
        DELETE FROM res_groups_users_rel
        WHERE gid = ANY(%s)
    """, (old_group_ids,))
