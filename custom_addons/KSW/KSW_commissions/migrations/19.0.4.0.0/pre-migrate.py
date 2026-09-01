"""Nine access-rights controls become two.

Runs BEFORE the new ``security.xml`` loads, while the groups it retires
still exist:

* ``group_commission_admin`` (the "Commission Configuration" dropdown) is
  merged into ``group_commission_officer``, which the new file renames to
  **Administrator**.  Without this its members would simply lose the
  catalog when the module update deletes the group at the end of the run —
  removing a ``<record>`` from the XML unlinks the row, and the m2m to
  ``res_users`` goes with it.
* the four tiers of the new **Commission Role** privilege are mutually
  exclusive, so a user carrying two of them directly (every accountant did:
  Accountant used to *imply* Officer, and several were also granted it
  outright) is reduced to the highest one.  The lower tiers arrive by
  implication where the ladder grants them; a leftover direct row would
  make the dropdown answer a different question than the record rules do.

The dead groups — ``group_commission_self`` and the three ``group_entry_*``
entry types — need nothing here.  They gate no ACL row, no record rule, no
view and no Python check, so the update can drop them and their members
lose nothing.
"""

import logging

_logger = logging.getLogger(__name__)


def _gid(cr, xmlid):
    cr.execute("""
        SELECT res_id FROM ir_model_data
         WHERE module = 'KSW_commissions' AND model = 'res.groups'
           AND name = %s
    """, (xmlid,))
    row = cr.fetchone()
    return row[0] if row else None


def _merge(cr, source, target):
    """Give every member of ``source`` the ``target`` group."""
    if not (source and target):
        return 0
    cr.execute("""
        INSERT INTO res_groups_users_rel (gid, uid)
             SELECT %s, uid FROM res_groups_users_rel WHERE gid = %s
        ON CONFLICT DO NOTHING
    """, (target, source))
    return cr.rowcount


def _collapse(cr, ladder):
    """Keep only the highest tier each user holds directly.

    ``ladder`` is ordered low to high; anything below the top tier a user
    has is deleted.
    """
    ids = [g for g in ladder if g]
    dropped = 0
    for position, lower in enumerate(ids[:-1]):
        higher = ids[position + 1:]
        cr.execute("""
            DELETE FROM res_groups_users_rel r
             WHERE r.gid = %s
               AND EXISTS (SELECT 1 FROM res_groups_users_rel h
                            WHERE h.uid = r.uid AND h.gid = ANY(%s))
        """, (lower, higher))
        dropped += cr.rowcount
    return dropped


def migrate(cr, version):
    if not version:
        return

    supervisor = _gid(cr, 'group_commission_supervisor')
    gm = _gid(cr, 'group_commission_gm')
    accountant = _gid(cr, 'group_commission_accountant')
    officer = _gid(cr, 'group_commission_officer')
    config = _gid(cr, 'group_commission_admin')
    sales_entry = _gid(cr, 'group_entry_sales')
    sales_manager = _gid(cr, 'group_sales_commission_manager')

    moved = _merge(cr, config, officer)

    role = _collapse(cr, [supervisor, gm, accountant, officer])
    sales = _collapse(cr, [sales_entry, sales_manager])

    cr.execute("SELECT count(*) FROM res_groups_users_rel WHERE gid = %s",
               (officer,))
    administrators = cr.fetchone()[0]

    _logger.info(
        "KSW_commissions 19.0.4.0.0: %s user(s) moved from Commission "
        "Configuration to Administrator (%s in total), %s redundant "
        "Commission Role row(s) and %s Sales & Collection row(s) dropped.",
        moved, administrators, role, sales,
    )
