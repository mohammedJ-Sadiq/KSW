"""19.0.3.1.0 — give every existing batch its department submission.

The handover record (``ksw.pay.submission``) is new, and the batches that
already exist have to land on the right side of it. The rule is simply what
their history already says:

* a batch inside an **approved or paid** month was handed over and approved —
  the month is locked, so the submission has to say so or the GM would see
  everything as outstanding;
* in an open month, a scope counts as handed over only when **all** of its
  batches are submitted. A half-typed department is still being prepared.

Nothing is deleted and no amount changes: this only classifies what is
already there, then rebuilds the register preview so the new "who gets paid"
page has something to show on the months still in progress.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})

    Batch = env['ksw.pay.batch'].sudo()
    batches = Batch.search([])
    if not batches:
        return

    # 1. Attach every batch to the submission for its scope, creating the
    #    month and the submission on the way through.
    for batch in batches:
        batch._ensure_submission()

    submissions = env['ksw.pay.submission'].sudo().search([])
    _logger.info(
        "19.0.3.1.0: attached %s batch(es) to %s department submission(s).",
        len(batches), len(submissions))

    # 2. Classify each one from the state of its batches and its month.
    submitted = approved = 0
    for submission in submissions:
        batch_states = set(submission.batch_ids.mapped('state'))
        if not batch_states:
            continue
        if (submission.run_id.state in ('approved', 'paid')
                or batch_states == {'approved'}):
            # Settled history — including the batches the 19.0.3.0.0
            # migration rebuilt from the old entry sheets, which it marked
            # approved precisely because they were already paid.
            submission.write({'state': 'approved'})
            approved += 1
        elif batch_states == {'submitted'}:
            # Credit the handover to whoever submitted the batches, so the
            # GM's list shows a name rather than an empty column.
            author = submission.batch_ids.mapped('submitted_by')[:1]
            dates = [d for d in submission.batch_ids.mapped('submitted_date')
                     if d]
            submission.write({
                'state': 'submitted',
                'submitted_by': author.id if author else False,
                'submitted_date': max(dates) if dates else False,
            })
            submitted += 1

    _logger.info(
        "19.0.3.1.0: %s submission(s) marked approved, %s submitted, "
        "%s still being prepared.",
        approved, submitted, len(submissions) - approved - submitted)

    # 3. Bring the months and their register previews up to date.
    #
    #    Safe on a month that already carries a register: the preview owns
    #    only the lines a previous preview created, and every line that came
    #    from the 19.0.3.0.0 migration is a settled figure it will not touch.
    runs = env['ksw.pay.run'].sudo().search([])
    runs._sync_state()
    runs._refresh_register()
    _logger.info("19.0.3.1.0: refreshed %s pay run(s).", len(runs))
