"""BAS Sales Reps: undo one wrong automatic flag, add one right one.

19.0.4.40.0 flagged "Company collector" (code 1001) No Commission from its
name. The March 2026 commission workbook shows it is a person, Mohamed
Fares, with a 0.5% / 0.75% bonus above 50% / 75% of target. Code 1006 is
"Sales manager", the holder of the defaulted 120309* accounts (المتعثر),
which the workbook never pays.

Only rows nobody has touched are changed: no employee linked.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE ksw_bas_sales_rep
           SET is_house = FALSE, state = 'todo', state_order = 0
         WHERE code = '1001' AND COALESCE(is_house, FALSE)
           AND employee_id IS NULL
    """)
    cr.execute("""
        UPDATE ksw_bas_sales_rep
           SET is_house = TRUE, state = 'house', state_order = 2
         WHERE code = '1006' AND NOT COALESCE(is_house, FALSE)
           AND employee_id IS NULL
    """)
