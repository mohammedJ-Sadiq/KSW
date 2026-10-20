"""Existing water branches keep the driver-led sequence.

`x_issue_mode` is new and not required, so the column holds NULL on every
existing operation type (KSW gotcha #146). The code already reads NULL as
'driver'; this only makes the Branches screen say so instead of showing a
blank switch.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE stock_picking_type
           SET x_issue_mode = 'driver'
         WHERE x_issue_mode IS NULL
    """)
