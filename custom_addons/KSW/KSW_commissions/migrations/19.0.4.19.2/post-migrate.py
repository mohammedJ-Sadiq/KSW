"""Batch and department totals in whole riyals.

``ksw.pay.batch.total_amount`` now rounds by the register's rule, and it is
stored, so every existing batch — and the department handovers that sum
them — is recomputed once here.

This also repairs totals that had simply gone stale: KSWCO batch 13
(August 2026, Maintenance) stored 4,900 over entries worth 5,000 after a
bulk rewrite of its entries on 2026-09-24 never reached the total.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    for model in ('ksw.pay.batch', 'ksw.pay.submission'):
        records = env[model].with_context(active_test=False).search([])
        fields_ = [f for f in env[model]._fields.values()
                   if f.compute == '_compute_totals' and f.store]
        for field in fields_:
            env.add_to_compute(field, records)
        records.flush_recordset()
        env.flush_all()
        _logger.info('KSW_commissions: %s totals recomputed on %d records.',
                     model, len(records))
