def migrate(cr, version):
    """The proposed manager / approvers / HR responsible are computed for every
    transfer on install, but a transfer closed before this version never applied
    them: clear them so it does not claim a change that did not happen."""
    cr.execute("""
        UPDATE ksw_employee_transfer
           SET to_manager_id = NULL, to_leave_manager_id = NULL,
               to_attendance_manager_id = NULL, to_hr_responsible_id = NULL
         WHERE state IN ('applied', 'refused', 'cancelled')
    """)
