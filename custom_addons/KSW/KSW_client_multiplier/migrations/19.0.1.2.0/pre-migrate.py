"""One client per contact (Oct 2026): clear duplicate contact links first.

`partner_id` becomes UNIQUE. Before, a contact could sit on several BAS
clients (one per BAS account linked to it). The lowest account code keeps the
contact; the others become unlinked BAS clients, their values untouched.
On dev the only case was a test contact a second, unrelated account had been
linked to by mistake.
"""


def migrate(cr, version):
    cr.execute("""
        UPDATE ksw_client_multiplier m SET partner_id = NULL
          FROM (
              SELECT id, ROW_NUMBER() OVER (
                         PARTITION BY partner_id
                         ORDER BY bas_code NULLS LAST, id) AS rn
                FROM ksw_client_multiplier
               WHERE partner_id IS NOT NULL
          ) d
         WHERE d.id = m.id AND d.rn > 1
    """)
