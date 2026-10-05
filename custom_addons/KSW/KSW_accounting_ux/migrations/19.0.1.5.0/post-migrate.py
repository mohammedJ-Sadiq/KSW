from odoo import SUPERUSER_ID, api

LEVEL_FIELDS = ('x_group_l1_id', 'x_group_l2_id', 'x_group_l3_id', 'x_group_l4_id')


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Line = env['account.move.line']
    # The related fields on journal items would otherwise be recomputed row
    # by row; the UPDATE below does it in one statement.
    with env.protecting([Line._fields[f] for f in LEVEL_FIELDS], Line.search([])):
        env['account.account'].with_context(active_test=False).search(
            [])._compute_ksw_group_levels()
        env.flush_all()
    cr.execute("""
        UPDATE account_move_line l
           SET x_group_l1_id = a.x_group_l1_id, x_group_l2_id = a.x_group_l2_id,
               x_group_l3_id = a.x_group_l3_id, x_group_l4_id = a.x_group_l4_id
          FROM account_account a
         WHERE a.id = l.account_id
    """)
