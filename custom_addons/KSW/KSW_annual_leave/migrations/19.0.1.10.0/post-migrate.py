"""Remove the obsolete "Remind Managers to Confirm Vacation Returns" cron.

August 2026. The daily manager chase is replaced by two enforcement paths
(supervisor confirmation of the attendance sheet, and an employee-side alert
when a fingerprint punch contradicts an open vacation), so the cron and its
two reminder-stamp columns are gone from the code.

Deleting the XML is not enough. The record was declared inside a
``noupdate="1"`` block, and — per the project's gotcha #3 — what a module
already wrote to the database survives the file that wrote it. Left alone,
the cron row keeps firing on a schedule and calls a method that no longer
exists, so every run raises AttributeError in the scheduler log.

The two dropped columns are deliberately NOT dropped from the table: Odoo
leaves orphaned columns in place on field removal, and keeping them costs
nothing while preserving the reminder history for anyone auditing why the
chase stopped.
"""

import logging

_logger = logging.getLogger(__name__)

XMLID = ('KSW_annual_leave', 'cron_return_confirmation_reminder')


def migrate(cr, version):
    if not version:
        return

    cr.execute(
        """
        SELECT id, res_id FROM ir_model_data
         WHERE module = %s AND name = %s AND model = 'ir.cron'
        """,
        XMLID,
    )
    row = cr.fetchone()
    if not row:
        _logger.info(
            'Return-confirmation reminder cron already absent — nothing to do.'
        )
        return

    data_id, cron_id = row

    # ir.cron is delegated to ir.actions.server (_inherits), so removing the
    # cron leaves the server action behind unless it goes too.
    cr.execute(
        'SELECT ir_actions_server_id FROM ir_cron WHERE id = %s', (cron_id,))
    action_row = cr.fetchone()

    cr.execute('DELETE FROM ir_cron WHERE id = %s', (cron_id,))
    if action_row and action_row[0]:
        cr.execute(
            'DELETE FROM ir_act_server WHERE id = %s', (action_row[0],))
    cr.execute('DELETE FROM ir_model_data WHERE id = %s', (data_id,))

    # ir.cron _inherits ir.actions.server, so loading the cron also created a
    # companion "<name>_ir_actions_server" xmlid for the delegated action.
    # Removing only the cron's own row leaves that one pointing at a deleted
    # record — an orphan that survives every later upgrade.
    cr.execute(
        """
        DELETE FROM ir_model_data
         WHERE module = %s AND name = %s AND model = 'ir.actions.server'
        """,
        (XMLID[0], '%s_ir_actions_server' % XMLID[1]),
    )

    _logger.info(
        'Removed obsolete return-confirmation reminder cron (id=%s).', cron_id)
