"""Create the level columns empty, so loading the module does not compute
them through the ORM (which recomputes every journal item in Python);
post-migrate fills them, the journal items in one UPDATE."""


def migrate(cr, version):
    for table in ('account_account', 'account_move_line'):
        for n in (1, 2, 3, 4):
            cr.execute(f"""
                ALTER TABLE {table} ADD COLUMN IF NOT EXISTS x_group_l{n}_id int4
                    REFERENCES account_group(id) ON DELETE SET NULL
            """)
