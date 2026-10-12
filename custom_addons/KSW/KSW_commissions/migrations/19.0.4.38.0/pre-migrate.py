# -*- coding: utf-8 -*-
"""One monthly run per General Manager (Oct 2026).

Drop the old one-run-per-month constraint before the module loads, so the
post-migrate can open a second run for the same month, and stamp each
existing run with the GM most of its departments answer to (the company
default GM where it has none).
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("ALTER TABLE ksw_pay_run "
               "DROP CONSTRAINT IF EXISTS ksw_pay_run_unique_period")
    cr.execute("ALTER TABLE ksw_pay_run "
               "ADD COLUMN IF NOT EXISTS gm_id integer")
    cr.execute("""
        UPDATE ksw_pay_run r SET gm_id = sub.gm_id
          FROM (SELECT DISTINCT ON (run_id) run_id, gm_id
                  FROM ksw_pay_submission
                 WHERE gm_id IS NOT NULL
                 GROUP BY run_id, gm_id
                 ORDER BY run_id, count(*) DESC, gm_id) sub
         WHERE sub.run_id = r.id AND r.gm_id IS NULL
    """)
    by_majority = cr.rowcount
    cr.execute("""
        UPDATE ksw_pay_run r SET gm_id = (
            SELECT e.user_id FROM res_company c
              JOIN hr_employee e ON e.id = c.x_default_gm_id
             WHERE e.user_id IS NOT NULL ORDER BY c.id LIMIT 1)
         WHERE r.gm_id IS NULL
    """)
    _logger.info(
        "KSW_commissions 19.0.4.38.0: GM stamped on %s run(s) from their "
        "departments, %s from the company default.", by_majority, cr.rowcount)
