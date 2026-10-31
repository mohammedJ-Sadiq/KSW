"""The customer's sales rep is Odoo's Salesperson (Oct 2026).

`res.partner.x_sales_rep_id` (an employee) was a second field for the same
fact as Odoo's Salesperson (`user_id`). Each value moves to the Salesperson
when the customer has none and the employee has an Odoo login; a rep with no
login cannot be a Salesperson and is logged for someone to decide. Read by
SQL: the field is gone from the code, its column is still there.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'res_partner' AND column_name = 'x_sales_rep_id'
    """)
    if not cr.fetchone():
        return
    cr.execute("""
        UPDATE res_partner p SET user_id = e.user_id
          FROM hr_employee e
         WHERE e.id = p.x_sales_rep_id AND p.user_id IS NULL AND e.user_id IS NOT NULL
     RETURNING p.id
    """)
    moved = cr.rowcount
    cr.execute("""
        SELECT p.id, p.name, e.name
          FROM res_partner p JOIN hr_employee e ON e.id = p.x_sales_rep_id
         WHERE e.user_id IS NULL
            OR (p.user_id IS NOT NULL AND p.user_id <> e.user_id)
    """)
    for partner_id, partner, rep in cr.fetchall():
        _logger.warning('KSW_commissions: sales rep %s of %s (id %s) was not moved to '
                        'Salesperson (no Odoo login, or a different Salesperson already '
                        'set). Set it by hand.', rep, partner, partner_id)
    _logger.info('KSW_commissions: %s sales reps moved to Salesperson.', moved)
