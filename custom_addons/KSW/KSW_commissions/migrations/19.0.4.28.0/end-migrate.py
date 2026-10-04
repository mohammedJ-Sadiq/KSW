"""Commissions are paid once: backfill the bank-file stamp, then latch EOS.

1. ``ksw.pay.run.line.x_bank_exported_date`` is new: from now on an exported
   bank file commits the month to the employee, and no vacation may settle
   it. Months whose file went out before this existed are recognised by the
   attachments the export always leaves on the run (``CommissionsBank_*`` /
   ``Commissions_*``; the BAS ``JournalEntry_*`` is not a payment). Every
   line of such a run is stamped with the run's first bank-file date — the
   safe side: a stamped line is paid by the run, never lost.

2. EOS requests now settle the recorded commission entries too. The latch is
   taken when a request *enters* Step 4, so an EOS request already sitting
   there would never get it. Take it now — after step 1, so a month already
   in a bank file is not latched onto the EOS payslip.

end-migrate, not post: the EOS flag lives in KSW_eos_leave, which loads
after this module (gotcha #45).
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def _backfill_bank_exports(env):
    cr = env.cr
    cr.execute("""
        SELECT a.res_id, MIN(a.create_date), MIN(a.create_uid)
          FROM ir_attachment a
          JOIN ksw_pay_run r ON r.id = a.res_id
         WHERE a.res_model = 'ksw.pay.run'
           AND r.state IN ('approved', 'paid')
           AND (a.name LIKE 'CommissionsBank\\_%%'
                OR a.name LIKE 'Commissions\\_%%')
         GROUP BY a.res_id
    """)
    stamped = 0
    for run_id, exported_on, exported_by in cr.fetchall():
        cr.execute("""
            UPDATE ksw_pay_run_line
               SET x_bank_exported_date = %s, x_bank_exported_by = %s
             WHERE run_id = %s AND x_bank_exported_date IS NULL
        """, (exported_on, exported_by, run_id))
        stamped += cr.rowcount
    _logger.info('KSW_commissions: stamped %s register line(s) as already '
                 'in an exported bank file.', stamped)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _backfill_bank_exports(env)
    env.invalidate_all()

    if 'is_eos_leave' not in env['hr.leave.type']._fields:
        _logger.info('KSW_commissions: KSW_eos_leave not installed, nothing to latch.')
        return
    leaves = env['hr.leave'].search([
        ('holiday_status_id.is_eos_leave', '=', True),
        ('x_annual_approval_state', '=', 'pending_acc'),
        ('x_commission_latched_date', '=', False),
    ]).filtered(lambda l: l._settles_commission_entries(l))
    leaves._latch_commission_entries()
    _logger.info('KSW_commissions: latched commission entries on %s EOS '
                 'request(s) at the accounting step: %s', len(leaves), leaves.ids)
