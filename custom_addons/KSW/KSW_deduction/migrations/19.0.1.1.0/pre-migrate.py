"""Move non-loan deductions from HR to the accounting data-entry team.

August 2026. Two renames, both done before the new XML loads:

1. ``managed_by`` value ``'hr'`` -> ``'acc_data_entry'`` on
   ``ksw.deduction.type`` and on the stored related column of
   ``ksw.deduction`` (record rules and the Deductions action filter on it).
2. The group xml id ``group_hr_deduction_officer`` ->
   ``group_acc_data_entry``. Renaming the ``ir.model.data`` row keeps the
   same ``res.groups`` record — every user already assigned to the role
   keeps it, and the loader then just updates its name/comment instead of
   creating a second group and dropping the old one.
"""

import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    cr.execute("""
        UPDATE ksw_deduction_type
           SET managed_by = 'acc_data_entry'
         WHERE managed_by = 'hr'
    """)
    type_rows = cr.rowcount

    cr.execute("""
        UPDATE ksw_deduction
           SET managed_by = 'acc_data_entry'
         WHERE managed_by = 'hr'
    """)
    deduction_rows = cr.rowcount

    cr.execute("""
        UPDATE ir_model_data
           SET name = 'group_acc_data_entry'
         WHERE module = 'KSW_deduction'
           AND model = 'res.groups'
           AND name = 'group_hr_deduction_officer'
           AND NOT EXISTS (
               SELECT 1 FROM ir_model_data existing
                WHERE existing.module = 'KSW_deduction'
                  AND existing.model = 'res.groups'
                  AND existing.name = 'group_acc_data_entry'
           )
    """)
    group_rows = cr.rowcount

    _logger.info(
        'KSW_deduction 19.0.1.1.0: managed_by hr -> acc_data_entry on '
        '%s type(s) and %s deduction(s); group xml id renamed: %s row(s)',
        type_rows, deduction_rows, group_rows,
    )
