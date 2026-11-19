def migrate(cr, version):
    """Transfers are no longer requested directly: the draft and GM steps are
    gone (the GM decides on the release before a transfer exists). A transfer
    still in either is put back with HR (GM step) or cancelled (draft)."""
    cr.execute("UPDATE ksw_employee_transfer SET state = 'hr' WHERE state = 'gm'")
    cr.execute("UPDATE ksw_employee_transfer SET state = 'cancelled' WHERE state = 'draft'")
