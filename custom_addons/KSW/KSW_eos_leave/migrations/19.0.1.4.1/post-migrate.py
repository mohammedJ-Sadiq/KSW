"""Freeze System Unpaid Days at 0 on EOS requests already past HR approval.

System-counted unpaid days apply only to requests HR has not approved yet
(decided with the user, 2026-10-01). Anything further along was signed off
under the old rule, where HR typed every unpaid day by hand, so its figure
must not move: freeze it at 0. Requests at DM or HR stay live; one returned
to HR later thaws through write() and is re-counted, as for any request.

Every EOS request that is not live (pending DM / HR, not validated) is frozen,
including refused and cancelled ones: freezing is harmless there and a reset
to draft thaws it.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE hr_leave l
           SET x_eos_system_unpaid_locked = TRUE,
               x_eos_system_unpaid_snapshot = 0
          FROM hr_leave_type t
         WHERE t.id = l.holiday_status_id
           AND t.is_eos_leave
           AND NOT (COALESCE(l.x_annual_approval_state, '')
                        IN ('pending_dm', 'pending_hr')
                    AND l.state NOT IN ('validate', 'validate1'))
     RETURNING l.id
    """)
    frozen = [r[0] for r in cr.fetchall()]
    _logger.info('EOS System Unpaid Days frozen at 0 on %d requests: %s',
                 len(frozen), frozen)
