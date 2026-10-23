"""Arabic names for the BAS branches, and a branch on every existing record.

The branch records are created by data/bas_branch_data.xml in this upgrade;
their Arabic names are set here because a .po entry is dropped unless the
.pot carries it too (CLAUDE.md gotcha #35). Only a name still equal to the
seeded English is translated, so a hand-made correction is kept.
"""
from odoo import SUPERUSER_ID, api

ARABIC = {
    '010': ('Head Office (General Ledger)', 'المركز الرئيسي (القيود العامة)'),
    '110': ('Head Office - Kawthar Office', 'المركز الرئيسي - مكتب الكوثر'),
    '115': ('Tyres Store - Isuzu', 'مستودع الكفرات - الايسوزو'),
    '120': ('Nabiya', 'النابية'),
    '125': ('Investment - Jubail Camps', 'الاستثمار - كمب الجبيل'),
    '135': ('Isuzu Sector', 'قطاع الايسوزو'),
    '140': ('Jubail', 'الجبيل'),
    '145': ('Manifa', 'منيفة'),
    '150': ('Tyres Store', 'مستودع الكفرات'),
    '152': ('Qiddiya', 'القدية'),
    '155': ('External Sales (Drivers)', 'المبيعات الخارجية (السائقين)'),
    '160': ('External Sales - Tariq', 'المبيعات الخارجية - طارق'),
    '165': ('Maintenance & Projects Store', 'مستودع الصيانة والمشاريع'),
    '170': ('Othmaniya', 'العثمانية'),
    '171': ('North Manifa', 'شمال منيفة'),
    '172': ('Scope Factory', 'مصنع سكوب'),
    '175': ('Al-Hayat Factory - Al-Ahsa', 'مصنع الحياة - الأحساء'),
    '180': ('Qairawan', 'القيروان'),
    '185': ('Tabuk 2', 'تبوك 2'),
    '190': ('Haradh', 'حرض'),
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for code, (english, arabic) in ARABIC.items():
        branch = env.ref('KSW_bas_gl_import.bas_branch_%s' % code, raise_if_not_found=False)
        if branch and branch.with_context(lang='en_US').name == english:
            branch.update_field_translations('name', {'ar_001': arabic})
    # Any code in use that the seed does not know, under its number until named.
    Branch = env['ksw.bas.branch']
    cr.execute("""
        SELECT DISTINCT TRIM(x_bas_branch) FROM account_move WHERE TRIM(x_bas_branch) <> ''
        UNION SELECT DISTINCT TRIM(x_bas_branch) FROM stock_picking WHERE TRIM(x_bas_branch) <> ''
    """)
    Branch._for_codes([r[0] for r in cr.fetchall()])
    env.flush_all()
    for table in ('account_move', 'stock_picking'):
        cr.execute(f"""
            UPDATE {table} t SET x_bas_branch_id = b.id
              FROM ksw_bas_branch b
             WHERE b.code = TRIM(t.x_bas_branch)
               AND t.x_bas_branch_id IS DISTINCT FROM b.id
        """)
    env.invalidate_all()
