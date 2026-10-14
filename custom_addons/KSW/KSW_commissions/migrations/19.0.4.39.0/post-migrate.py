"""Reopen departments closed by a whole-month Submit right after Send Early.

Submit to GM used to default to "every component". On KSWCO, Maintenance
and Supply Chain · Oct 2026 sent one employee early in sub-batches and then
pressed it, so the GM's approval closed October for everybody else in them
(batches approved, no new batch allowed, the paid rows unreopenable).

A department goes back to where Send Early meant to leave it (being
prepared, its batches open) when it is approved, every one of its rows came
through a sub-batch, and its month has not been finalised. The rows keep
their approval and payment, and the sub-batches stay approved. Idempotent.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    candidates = env['ksw.pay.submission'].search([
        ('state', '=', 'approved'),
        ('run_id.state', 'not in', ('approved', 'paid')),
    ])
    # Per row, not all(mapped(...)): mapped() returns the sub-batches, and
    # all() over an empty set is True, which would reopen every approved
    # department that never had a sub-batch (caught in rehearsal on a copy of
    # KSWCO: four September departments).
    reopened = candidates.filtered(
        lambda s: s.batch_ids.entry_ids
        and all(e.x_sub_batch_id for e in s.batch_ids.entry_ids))
    for submission in reopened:
        # ksw_entry_sync: a bare state write on a batch drags its rows along,
        # and these rows stay approved.
        submission.batch_ids.filtered(lambda b: b.state != 'draft') \
            .with_context(ksw_entry_sync=True).write({
                'state': 'draft',
                'submitted_by': False,
                'submitted_date': False,
                'handed_over_date': False,
            })
        submission.write({
            'state': 'draft',
            'submitted_by': False,
            'submitted_date': False,
        })
        submission.message_post(
            body="Reopened: only employees sent early were in this "
                 "department when the whole month was submitted. Their "
                 "approved entries are unchanged; the rest of the month is "
                 "open again.",
            subtype_xmlid='mail.mt_note')
        _logger.info('Reopened %s (%s batches)', submission.display_name,
                     len(submission.batch_ids))
    runs = reopened.run_id
    runs._sync_state()
    runs._refresh_register()
    _logger.info('Departments reopened: %s', len(reopened))
